import copy
import io
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4
from sqlalchemy.dialects import postgresql
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from app.models import (ProcessingRun, Task, TaskEvidence, ExtractionGeneration, GenerationPart,
                        SourceChunk, ChunkEmbedding)
from app.schemas.hierarchical import ConsolidatedExtraction
from app.schemas.reasoning import QueryPlan
from app.services.reasoning import retrieve, temporal_column
from app.services.hierarchical_llm import validate_consolidation
from app.services.processing import process_text_source, SourceNotProcessable
from app.services.memory import ensure_source_chunks, semantic_statement
from app.services.chunking import chunk_version, legacy_chunk_version
from app.services.claude import ExtractionError
import test_hierarchical as fixtures
from test_hierarchical import FakeSession, FakeClaude, config, QUOTE


class EvolutionTests(unittest.TestCase):
    def test_temporal_queries_choose_received_due_created_and_decided_columns(self):
        now = datetime(2026, 10, 5, tzinfo=timezone.utc)
        for basis, task_column, decision_column in (
            ('source_date', 'sources.received_at', 'sources.received_at'),
            ('due_date', 'tasks.due_at', None),
            ('created_date', 'tasks.created_at', 'decisions.created_at'),
            ('decision_date', None, 'decisions.decided_at'),
            ('mixed', 'tasks.due_at', 'coalesce(decisions.decided_at, sources.received_at)'),
            ('none', None, None)):
            session = MagicMock()
            session.execute.return_value.all.return_value = []
            session.scalars.return_value.all.return_value = []
            retrieve(session, QueryPlan(time_basis=basis, date_from=now,
                date_to=now + timedelta(days=7), include_recent_sources=False), config())
            for call, column in zip(session.execute.call_args_list, (task_column, decision_column)):
                sql = str(call.args[0].compile(dialect=postgresql.dialect()))
                if column:
                    self.assertIn(column + ' >=', sql)
                    self.assertIn(column + ' <=', sql)
                else:
                    self.assertNotIn(' >= ', sql)
        with self.assertRaises(ValidationError):
            QueryPlan(time_basis='arbitrary SQL')

    def test_assumed_this_week_is_different_from_due_this_week_and_null_decision_date(self):
        now = datetime(2026, 10, 5, tzinfo=timezone.utc)
        recent = SimpleNamespace(received_at=now, created_at=now, due_at=now + timedelta(days=30))
        old = SimpleNamespace(received_at=now - timedelta(days=30), created_at=now - timedelta(days=30), due_at=now)
        window = lambda value: value is not None and now <= value < now + timedelta(days=7)
        self.assertTrue(window(recent.received_at))
        self.assertFalse(window(recent.due_at))
        self.assertFalse(window(old.received_at))
        self.assertTrue(window(old.due_at))
        self.assertEqual(str(temporal_column('tasks', 'source_date')), 'Source.received_at')
        self.assertEqual(str(temporal_column('tasks', 'due_date')), 'Task.due_at')
        self.assertEqual(str(temporal_column('decisions', 'source_date')), 'Source.received_at')
        self.assertIn('coalesce', str(temporal_column('decisions', 'mixed')))

    def test_embedding_change_reuses_existing_chunk_ids_and_partial_extraction(self):
        session, claude = FakeSession(), FakeClaude()
        with patch('app.services.hierarchical_llm.call_json', side_effect=claude):
            process_text_source(session, session.source.id, config())
            original_ids = [chunk.id for chunk in session.chunks]
            calls = claude.part_calls
            process_text_source(session, session.source.id, config(
                OPENROUTER_EMBEDDING_MODEL='new-model', OPENROUTER_EMBEDDING_DIMENSIONS=3), force=True)
        self.assertEqual(original_ids, [chunk.id for chunk in session.chunks])
        self.assertEqual(claude.part_calls, calls)
        self.assertEqual(claude.consolidation_calls, 2)

    def test_legacy_chunk_alias_keeps_ids_text_offsets_and_physical_version(self):
        session = FakeSession(raw='legacy text')
        old_version = legacy_chunk_version(config(OPENROUTER_EMBEDDING_MODEL='old-model'))
        chunk = SourceChunk(id=uuid4(), source_id=session.source.id, chunk_version=old_version,
            chunk_index=0, char_start=0, char_end=len(session.source.raw_content), content=session.source.raw_content)
        session.chunks = [chunk]
        with session.begin():
            version = ensure_source_chunks(session, session.source, config(OPENROUTER_EMBEDDING_MODEL='new-model'))
        self.assertEqual(version, old_version)
        self.assertEqual(chunk.logical_version, chunk_version(config()))
        self.assertEqual(session.chunks, [chunk])

    def test_refresh_creates_new_generation_preserves_base_and_reprocess_reuses_published(self):
        session, claude = FakeSession(), FakeClaude()
        with patch('app.services.hierarchical_llm.call_json', side_effect=claude):
            process_text_source(session, session.source.id, config())
            previous_run = session.source.latest_processing_run_id
            original = copy.deepcopy({key: part.result for key, part in session.parts.items()})
            previous_calls = claude.part_calls
            process_text_source(session, session.source.id, config(), force=True, refresh_parts=True)
            self.assertEqual(claude.part_calls - previous_calls, len(session.chunks))
            generations = [row for row in session.rows if isinstance(row, ExtractionGeneration)]
            refreshed = [row for row in session.rows if isinstance(row, GenerationPart)]
            self.assertEqual(len(generations), 1)
            self.assertEqual(generations[0].previous_run_id, previous_run)
            self.assertEqual(generations[0].status, 'processed')
            self.assertEqual(len(refreshed), len(session.chunks))
            after = claude.part_calls
            process_text_source(session, session.source.id, config(), force=True)
        self.assertEqual(claude.part_calls, after)
        self.assertEqual(original, {key: part.result for key, part in session.parts.items()})
        self.assertEqual(len([row for row in session.rows if isinstance(row, ProcessingRun)]), 3)
        latest = [row for row in session.rows if isinstance(row, ProcessingRun)][-1]
        self.assertEqual(set(latest.result['part_ids']), {str(row.id) for row in refreshed})
        self.assertTrue(any(row.generation_part_id for row in session.rows if isinstance(row, TaskEvidence)))

    def test_refresh_failed_part_resumes_same_generation_and_preserves_previous_success(self):
        session, claude = FakeSession(), FakeClaude()
        with patch('app.services.hierarchical_llm.call_json', side_effect=claude):
            process_text_source(session, session.source.id, config())
            previous = session.source.latest_processing_run_id
            claude.fail_part = claude.part_calls + 3
            with self.assertRaises(ExtractionError):
                process_text_source(session, session.source.id, config(), force=True, refresh_parts=True)
            self.assertEqual(session.source.latest_processing_run_id, previous)
            self.assertEqual(len([row for row in session.rows if isinstance(row, GenerationPart)]), 2)
            before = claude.part_calls
            claude.fail_part = None
            process_text_source(session, session.source.id, config(), force=True, refresh_parts=True)
        self.assertEqual(claude.part_calls - before, len(session.chunks) - 2)
        self.assertEqual(len([row for row in session.rows if isinstance(row, ExtractionGeneration)]), 1)

    def test_refresh_requires_reprocess_before_paid_call(self):
        session = FakeSession()
        with patch('app.services.hierarchical_llm.call_json') as paid:
            with self.assertRaises(SourceNotProcessable):
                process_text_source(session, session.source.id, config(), refresh_parts=True)
        paid.assert_not_called()

    def test_rejected_candidate_is_traced_and_never_becomes_final_task(self):
        session, data, candidates, final = fixtures.HierarchicalTests().candidate_fixture()
        ref = next(iter(candidates['tasks']))
        final['tasks'] = []
        final['dispositions'] = [{'candidate_id': ref, 'status': 'rejected',
            'reason': 'idea_not_commitment', 'final_index': None}]
        result, provenance = validate_consolidation(ConsolidatedExtraction.model_validate(final),
            candidates, session.source, session.projects)
        self.assertEqual(result.tasks, [])
        self.assertEqual(provenance['tasks'], [])
        self.assertEqual(provenance['dispositions'][0]['reason'], 'idea_not_commitment')
        self.assertEqual(len(candidates['tasks']), 1)
        final['dispositions'][0]['reason'] = ' '
        with self.assertRaises(ValidationError):
            ConsolidatedExtraction.model_validate(final)

    def test_duplicate_dispositions_or_missing_evaluation_are_rejected(self):
        session, data, candidates, final = fixtures.HierarchicalTests().candidate_fixture()
        for dispositions in ([], final['dispositions'] * 2):
            invalid = dict(final, dispositions=dispositions)
            with self.assertRaises(ExtractionError):
                validate_consolidation(ConsolidatedExtraction.model_validate(invalid), candidates,
                    session.source, session.projects)

    def test_overlap_dedup_remaps_disposition_to_actual_final_index(self):
        session, data, candidates, final = fixtures.HierarchicalTests().candidate_fixture()
        first = next(iter(candidates['tasks']))
        second = 'tasks:second:0'
        candidates['tasks'][second] = copy.deepcopy(candidates['tasks'][first])
        final['tasks'].append(dict(final['tasks'][0], candidate_ids=[second]))
        final['dispositions'].append({'candidate_id': second, 'status': 'kept', 'final_index': 1, 'reason': None})
        result, provenance = validate_consolidation(ConsolidatedExtraction.model_validate(final), candidates,
            session.source, session.projects)
        self.assertEqual(len(result.tasks), 1)
        self.assertEqual(provenance['dispositions'][1]['final_index'], 0)
        self.assertEqual(provenance['dispositions'][1]['status'], 'merged')

    def test_new_migrations_are_additive_local_vector_copy_and_immutable_memory(self):
        output = io.StringIO()
        with patch('app.config.get_settings', return_value=config(DATABASE_URL='postgresql://offline/db')):
            command.upgrade(Config('alembic.ini', output_buffer=output), '0007_hierarchical_extraction:head', sql=True)
        sql = output.getvalue()
        for table in ('chunk_embeddings', 'extraction_generations', 'extraction_generation_parts',
                      'project_memory_versions', 'project_memory_state', 'project_memory_events'):
            self.assertIn('CREATE TABLE ' + table, sql)
        self.assertIn('INSERT INTO chunk_embeddings', sql)
        self.assertIn('SELECT', sql)
        self.assertIn('ON CONFLICT', sql)
        self.assertIn('preserve_project_memory_history', sql)
        for forbidden in ('DROP ', 'TRUNCATE ', 'DELETE FROM ', 'UPDATE sources', 'UPDATE processing_run_parts'):
            self.assertNotIn(forbidden, sql)
        self.assertIn('chunk_embeddings', str(semantic_statement([1, 0, 0], config(
            OPENROUTER_EMBEDDING_MODEL='active-model', OPENROUTER_EMBEDDING_DIMENSIONS=3))))
    def test_rejected_candidates_remain_in_parts_without_persisting_final_tasks(self):
        repo, claude = FakeSession(), FakeClaude()
        def reject(settings, system, data, schema, client=None, **kwargs):
            result = claude(settings, system, data, schema, client, **kwargs)
            if schema is ConsolidatedExtraction:
                payload = result.model_dump(mode='json')
                payload['tasks'] = []
                payload['dispositions'] = [dict(item, status='rejected', final_index=None,
                    reason='idea_not_commitment') for item in payload['dispositions']]
                return ConsolidatedExtraction.model_validate(payload)
            return result
        with patch('app.services.hierarchical_llm.call_json', side_effect=reject):
            response = process_text_source(repo, repo.source.id, config())
        self.assertEqual(response['tasks_count'], 0)
        self.assertFalse(any(isinstance(row, Task) for row in repo.rows))
        self.assertTrue(any(part.result['extraction']['tasks'] for part in repo.parts.values()))
        run = next(row for row in repo.rows if isinstance(row, ProcessingRun))
        self.assertTrue(all(item['reason'] == 'idea_not_commitment' for item in run.result['candidate_dispositions']))