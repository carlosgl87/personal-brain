import copy
import io
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from fastapi.testclient import TestClient
from app.config import get_settings
from app.database import get_session
from app.main import app
from app.models import (Project, Source, Task, TaskChange, ProjectUpdate, ProjectMemoryState, ProjectMemoryVersion, ProjectMemoryEvent)
from app.schemas.project_memory import ProjectMemoryContent
from app.schemas.reasoning import QueryPlan, SynthesizedAnswer
from app.services.project_memory_events import mark_dirty, schedule_refresh, parse_refresh
from app.services.project_memory import snapshot_data, publish_memory, due_reconciliation, candidates, refresh_project
from app.services.project_memory_llm import generate_memory, validate_memory
from app.services.project_memory_retrieval import retrieve_project_memories
from app.services.reasoning import retrieve, trace_context
from app.services.reasoning_llm import ReasoningError, synthesize
from app.services.queries import Scope
from app.services.task_management import TaskCommand, edit_task
from app.project_memory_worker import run_once
from test_hierarchical import config, FakeSession, FakeClaude

NOW = datetime(2026, 10, 6, 15, tzinfo=timezone.utc)


def settings(**values):
    return config(**({'PROJECT_MEMORY_ENABLED': True} | values))


def state(**values):
    defaults = dict(project_id=uuid4(), current_version_id=None, is_dirty=False, dirty_since=None,
        refresh_after=None, retry_after=None, last_change_at=NOW, change_revision=0,
        reconciled_revision=0, last_incremental_at=None, last_reconciliation_at=None)
    return SimpleNamespace(**(defaults | values))


def memory(source_id, text='Dashboard pendiente.'):
    return ProjectMemoryContent(current_state={'text': text, 'source_ids': [source_id]})


class EventSession:
    def __init__(self, item):
        self.item, self.events, self.locked = item, [], []
    def execute(self, statement):
        self.insert_sql = str(statement.compile(dialect=postgresql.dialect()))
    def scalar(self, statement):
        params = statement.compile(dialect=postgresql.dialect()).params
        entity = statement.column_descriptions[0]['entity']
        if entity is ProjectMemoryState:
            self.locked.append(statement._for_update_arg is not None)
            return self.item
        return next((e.id for e in self.events if e.origin_key == params['origin_key_1']), None)
    def add(self, event):
        self.events.append(event)


class ProjectMemoryTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.clear()

    def test_debounce_moves_with_each_source_but_duplicate_is_idempotent(self):
        item = state()
        session = EventSession(item)
        first, second = uuid4(), uuid4()
        mark_dirty(session, item.project_id, settings(), origin_key='run:1', source_id=first, now=NOW)
        mark_dirty(session, item.project_id, settings(), origin_key='run:2', source_id=second, now=NOW + timedelta(minutes=2))
        self.assertEqual(item.refresh_after, NOW + timedelta(minutes=7))
        self.assertEqual(item.dirty_since, NOW)
        self.assertEqual(item.change_revision, 2)
        self.assertEqual([e.source_id for e in session.events], [first, second])
        self.assertTrue(item.is_dirty)
        self.assertTrue(all(session.locked))
        deadline = item.refresh_after
        self.assertFalse(mark_dirty(session, item.project_id, settings(), origin_key='run:2', now=NOW + timedelta(minutes=3)))
        self.assertEqual(item.refresh_after, deadline)
        self.assertEqual(len(session.events), 2)

    def test_disabled_or_missing_project_never_schedules_or_calls_provider(self):
        session = MagicMock()
        self.assertFalse(mark_dirty(session, uuid4(), settings(PROJECT_MEMORY_ENABLED=False), origin_key='x'))
        self.assertFalse(mark_dirty(session, None, settings(), origin_key='x'))
        session.execute.assert_not_called()
        with patch('app.services.project_memory.generate_memory') as paid:
            self.assertFalse(refresh_project(MagicMock(), uuid4(), settings(PROJECT_MEMORY_ENABLED=False)))
        paid.assert_not_called()

    def snapshot_fixture(self, previous=False, event_count=2, max_sources=20):
        item = state(change_revision=event_count, is_dirty=True)
        source_ids = [uuid4() for _ in range(event_count)]
        previous_version = SimpleNamespace(id=uuid4(), version_number=1, through_revision=0,
            memory=memory(source_ids[0]).model_dump(mode='json')) if previous else None
        if previous:
            item.current_version_id = previous_version.id
        events = [SimpleNamespace(revision=i + 1, created_at=NOW + timedelta(seconds=i),
            source_id=sid, task_id=None, command_source_id=None, event_type='processed_source')
            for i, sid in enumerate(source_ids)]
        source_rows = [(SimpleNamespace(id=sid, received_at=NOW, source_type='meeting_transcript',
            raw_content='Actual evidence ' + str(i)), SimpleNamespace(id=uuid4(), result={'summary': 'Latest summary'}))
            for i, sid in enumerate(source_ids)]
        session = MagicMock()
        def get(model, identifier):
            return {ProjectMemoryState: item, Project: SimpleNamespace(id=item.project_id, name='SIMA'),
                    ProjectMemoryVersion: previous_version}.get(model)
        session.get.side_effect = get
        session.scalars.side_effect = [SimpleNamespace(all=lambda: events[:max_sources]), SimpleNamespace(all=lambda: []), SimpleNamespace(all=lambda: [])]
        session.execute.side_effect = [SimpleNamespace(all=lambda: source_rows[:max_sources]), SimpleNamespace(all=lambda: [])]
        return item, session, source_rows, previous_version

    def test_first_memory_batches_sources_and_includes_exact_cursor(self):
        item, session, rows, _ = self.snapshot_fixture()
        captured, data = snapshot_data(session, item.project_id, settings())
        self.assertEqual(len(data['sources']), 2)
        self.assertEqual(captured.cursor, 2)
        self.assertEqual(captured.version_number, 1)
        self.assertIsNone(data['previous_memory'])
        self.assertEqual([x['source_id'] for x in data['sources']], [str(s.id) for s, _ in rows])
        query = str(session.execute.call_args_list[0].args[0].compile(dialect=postgresql.dialect()))
        self.assertIn('sources.id IN', query)

    def test_updates_are_bounded_inputs_and_old_memory_is_unchanged(self):
        item, session, rows, previous = self.snapshot_fixture(previous=True)
        old = copy.deepcopy(previous.memory)
        updates = [SimpleNamespace(id=uuid4(), source_id=rows[0][0].id,
            update_text='Nuevo estado ' + str(i), event_at=NOW) for i in range(41)]
        session.scalars.side_effect = [SimpleNamespace(all=lambda: []), SimpleNamespace(all=lambda: []),
                                       SimpleNamespace(all=lambda: updates)]
        session.execute.side_effect = [SimpleNamespace(all=lambda: [])]
        captured, data = snapshot_data(session, item.project_id, settings())
        self.assertEqual(len(data['updates']), 40)
        self.assertTrue(data['warnings'])
        sql = str(session.scalars.call_args_list[2].args[0].compile(dialect=postgresql.dialect()))
        self.assertIn('project_updates.processing_run_id = sources.latest_processing_run_id', sql)
        self.assertIn('LIMIT', sql)
        cfg = settings(PROJECT_MEMORY_MAX_CHARS=3000)
        with patch('app.services.project_memory_llm.call_json', return_value=memory(rows[0][0].id, 'Estado actual compacto.')):
            content = generate_memory(cfg, data)
        self.assertLess(len(json.dumps(content)), 3000)
        self.assertEqual(previous.memory, old)
        self.assertNotIn('Nuevo estado 39', json.dumps(content))

    def test_multiple_natural_changes_from_one_source_are_visible_in_delta(self):
        item, session, rows, previous = self.snapshot_fixture(previous=True)
        sid = rows[0][0].id
        changes = [SimpleNamespace(id=uuid4(), before={'status': 'open'}, after={'status': 'completed'},
                                   action='natural_completion') for _ in range(2)]
        events = [SimpleNamespace(revision=i+1, created_at=NOW, source_id=sid, task_id=uuid4(), command_source_id=sid,
            event_type='task_change', origin_key='task:' + str(c.id)) for i, c in enumerate(changes)]
        session.scalars.side_effect = [SimpleNamespace(all=lambda: events), SimpleNamespace(all=lambda: []),
                                       SimpleNamespace(all=lambda: [])]
        original_get = session.get.side_effect
        session.get.side_effect = lambda model, identifier: next(c for c in changes if c.id == identifier) if model is TaskChange else original_get(model, identifier)
        _, data = snapshot_data(session, item.project_id, settings())
        self.assertEqual(len(data['changes']), 2)
        self.assertTrue(all(c['after']['status'] == 'completed' for c in data['changes']))
    def test_incremental_reads_previous_memory_and_only_unconsumed_events(self):
        item, session, rows, previous = self.snapshot_fixture(previous=True)
        captured, data = snapshot_data(session, item.project_id, settings())
        self.assertEqual(data['previous_memory'], previous.memory)
        self.assertEqual(captured.previous_id, previous.id)
        self.assertEqual(captured.version_number, 2)
        sql = str(session.scalars.call_args_list[0].args[0].compile(dialect=postgresql.dialect()))
        self.assertIn('project_memory_events.revision >', sql)
        self.assertIn('project_memory_events.revision <=', sql)
        self.assertEqual(captured.cursor, 2)

    def test_large_batch_advances_only_consumed_cursor_not_all_changes(self):
        item, session, rows, _ = self.snapshot_fixture(event_count=4, max_sources=2)
        captured, data = snapshot_data(session, item.project_id, settings(PROJECT_MEMORY_INCREMENTAL_MAX_SOURCES=2))
        self.assertEqual(captured.cursor, 2)
        self.assertEqual(captured.revision, 4)
        self.assertEqual(len(data['sources']), 2)
        session.scalar.return_value = item
        publish_memory(session, item.project_id, captured, data,
            memory(rows[0][0].id).model_dump(mode='json'), settings(), False, NOW)
        self.assertTrue(item.is_dirty)
        self.assertEqual(item.refresh_after, NOW)

    def test_versioning_preserves_previous_and_new_events_keep_their_debounce(self):
        item, session, rows, previous = self.snapshot_fixture(previous=True)
        captured, data = snapshot_data(session, item.project_id, settings())
        old_memory = copy.deepcopy(previous.memory)
        item.change_revision += 1
        item.refresh_after = NOW + timedelta(minutes=5)
        session.scalar.return_value = item
        value = memory(rows[0][0].id, 'Dashboard completado.').model_dump(mode='json')
        identifier = publish_memory(session, item.project_id, captured, data, value, settings(), False, NOW)
        version = session.add.call_args.args[0]
        self.assertIsInstance(version, ProjectMemoryVersion)
        self.assertEqual(version.previous_version_id, previous.id)
        self.assertEqual(version.version_number, 2)
        self.assertEqual(previous.memory, old_memory)
        self.assertEqual(item.current_version_id, identifier)
        self.assertTrue(item.is_dirty)
        self.assertEqual(item.refresh_after, NOW + timedelta(minutes=5))
        self.assertEqual(version.through_revision, 2)
        self.assertEqual(version.trigger_source_ids, [str(s.id) for s, _ in rows])

    def test_clean_publication_clears_dirty_without_erasing_history(self):
        item, session, rows, _ = self.snapshot_fixture()
        captured, data = snapshot_data(session, item.project_id, settings())
        session.scalar.return_value = item
        publish_memory(session, item.project_id, captured, data,
            memory(rows[0][0].id).model_dump(mode='json'), settings(), True, NOW)
        self.assertFalse(item.is_dirty)
        self.assertIsNone(item.refresh_after)
        self.assertEqual(item.reconciled_revision, 2)
        self.assertEqual(item.last_reconciliation_at, NOW)
        self.assertEqual(session.add.call_count, 1)

    def test_publication_rejects_concurrently_changed_pointer(self):
        item, session, _, _ = self.snapshot_fixture()
        captured, data = snapshot_data(session, item.project_id, settings())
        item.current_version_id = uuid4()
        session.scalar.return_value = item
        with self.assertRaises(ReasoningError):
            publish_memory(session, item.project_id, captured, data, {}, settings(), False, NOW)
        session.add.assert_not_called()

    def test_provenance_and_size_are_validated_not_silently_truncated(self):
        source_id = uuid4()
        result = memory(source_id)
        self.assertEqual(validate_memory(result, {str(source_id)}, settings())['current_state']['source_ids'], [str(source_id)])
        with self.assertRaises(ReasoningError):
            validate_memory(result, {str(uuid4())}, settings())
        oversized = memory(source_id, 'x' * 2000)
        with self.assertRaises(ReasoningError):
            validate_memory(oversized, {str(source_id)}, settings(PROJECT_MEMORY_MAX_CHARS=1000))
        with self.assertRaises(ValidationError):
            ProjectMemoryContent(current_state={'text': 'invented state', 'source_ids': []})

    def test_new_evidence_can_replace_obsolete_state_with_grounded_version(self):
        source_id, newer_id = uuid4(), uuid4()
        old = memory(source_id).model_dump(mode='json')
        data = {'previous_memory': old, 'sources': [{'source_id': str(newer_id), 'excerpt': 'Dashboard completado.'}],
            'tasks': [{'source_id': str(newer_id), 'status': 'completed'}], 'changes': []}
        result = memory(newer_id, 'Dashboard completado.')
        with patch('app.services.project_memory_llm.call_json', return_value=result) as paid:
            content = generate_memory(settings(), data)
        self.assertEqual(content['current_state']['text'], 'Dashboard completado.')
        self.assertEqual(old['current_state']['text'], 'Dashboard pendiente.')
        self.assertEqual(paid.call_args.args[2]['tasks'][0]['status'], 'completed')
        self.assertIn('obsoletos', paid.call_args.args[1])
    def stale_fixture(self, global_scope=False, completed=False):
        sid, command_id = uuid4(), uuid4()
        item = state(current_version_id=uuid4(), is_dirty=True, change_revision=2)
        version = SimpleNamespace(id=item.current_version_id, version_number=1, through_revision=1,
            consolidated_through_at=NOW - timedelta(minutes=10), memory=memory(sid).model_dump(mode='json'))
        event = SimpleNamespace(revision=2, source_id=sid, task_id=uuid4(), command_source_id=command_id if completed else None,
            event_type='task_change' if completed else 'processed_source', origin_key='task:' + str(uuid4()))
        session = MagicMock()
        session.execute.side_effect = [SimpleNamespace(all=lambda: [(item, version, 'SIMA')]),
            SimpleNamespace(all=lambda: [(SimpleNamespace(id=sid, received_at=NOW,
                raw_content='Dashboard completado.'), SimpleNamespace(id=uuid4(), result={'summary': 'Completado.'}))])]
        session.scalars.return_value.all.return_value = [event]
        session.get.side_effect = lambda model, identifier: SimpleNamespace(before={'status': 'open'}, after={'status': 'completed'}, action='completar') if model is TaskChange else SimpleNamespace(title='Dashboard', completed_at=NOW, status='completed', project_id=item.project_id, owner_text='Ana', due_at=None)
        scope = Scope(None if global_scope else [item.project_id], 'SIMA')
        return session, scope, sid, command_id

    def test_stale_memory_retrieval_includes_new_source_during_debounce(self):
        session, scope, sid, _ = self.stale_fixture()
        memories, deltas, warnings = retrieve_project_memories(session, scope, settings())
        self.assertEqual(memories[0]['memory']['current_state']['text'], 'Dashboard pendiente.')
        self.assertEqual(deltas[0]['excerpt'], 'Dashboard completado.')
        self.assertEqual(deltas[0]['source_id'], str(sid))
        self.assertTrue(any('pendiente de actualizacion' in value for value in warnings))

    def test_completed_task_delta_contains_current_sql_even_when_open_tasks_list_omits_it(self):
        session, scope, _, command_id = self.stale_fixture(completed=True)
        _, deltas, _ = retrieve_project_memories(session, scope, settings())
        event_delta = next(value for value in deltas if value['source_id'] == str(command_id))
        self.assertEqual(event_delta['current_task']['status'], 'completed')
        self.assertEqual(event_delta['after']['status'], 'completed')

    def test_reasoning_combines_memory_delta_and_current_structure(self):
        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        session.execute.return_value.all.return_value = []
        mem = {'memory_version_id': str(uuid4()), 'source_ids': [str(uuid4())], 'memory': {'current_state': {'text': 'Older state'}}}
        delta = {'source_id': str(uuid4()), 'excerpt': 'Latest state'}
        with patch('app.services.reasoning.retrieve_project_memories', return_value=([mem], [delta], ['stale'])):
            result = retrieve(session, QueryPlan(), settings())
        self.assertEqual(result['project_memories'], [mem])
        self.assertEqual(result['new_sources'], [delta])
        trace = json.dumps(trace_context(result))
        self.assertNotIn('Older state', trace)
        self.assertNotIn('Latest state', trace)
        self.assertIn(mem['memory_version_id'], trace)
        self.assertIn(delta['source_id'], trace)

    def test_no_memory_uses_existing_sql_fallback(self):
        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        task = SimpleNamespace(id=uuid4(), source_id=uuid4(), processing_run_id=None,
            title='Current SQL task', description=None, owner_text='Ana', status='open', due_at=None, completed_at=None)
        session.execute.side_effect = [SimpleNamespace(all=lambda: []), SimpleNamespace(all=lambda: [(task, 'SIMA')]),
            SimpleNamespace(all=lambda: []), SimpleNamespace(all=lambda: []), SimpleNamespace(all=lambda: [])]
        result = retrieve(session, QueryPlan(), settings())
        self.assertEqual(result['project_memories'], [])
        self.assertEqual(result['tasks'][0]['title'], 'Current SQL task')

    def test_temporal_window_excludes_undated_current_memory_to_avoid_history_leakage(self):
        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        session.execute.return_value.all.return_value = []
        with patch('app.services.reasoning.retrieve_project_memories') as memories:
            result = retrieve(session, QueryPlan(time_basis='source_date', date_from=NOW), settings())
        memories.assert_not_called()
        self.assertEqual(result['project_memories'], [])

    def test_synthesis_accepts_original_source_refs_from_memory_and_delta_only(self):
        sid, new_id = uuid4(), uuid4()
        context = {'project_memories': [{'source_ids': [str(sid)]}], 'new_sources': [{'source_id': str(new_id)}]}
        answer = SynthesizedAnswer(text='Estado con evidencia.', source_ids=[sid, new_id])
        with patch('app.services.reasoning_llm.call_json', return_value=answer):
            response = synthesize('Estado?', context, settings())
        self.assertIn(str(sid), response)
        self.assertIn(str(new_id), response)

    def test_global_memories_are_compact_and_multi_project(self):
        session = MagicMock()
        versions = []
        for name in ('SIMA', 'AI Tutor'):
            item = state(current_version_id=uuid4())
            value = memory(uuid4(), 'x' * 2000).model_dump(mode='json')
            versions.append((item, SimpleNamespace(id=item.current_version_id, version_number=1, through_revision=0,
                consolidated_through_at=NOW, memory=value), name))
        session.execute.return_value.all.return_value = versions
        memories, _, _ = retrieve_project_memories(session, Scope(None, 'Todos'), settings())
        self.assertEqual({item['project'] for item in memories}, {'SIMA', 'AI Tutor'})
        self.assertTrue(all(len(item['memory']['current_state']['text']) == 350 for item in memories))
        self.assertTrue(all(item['memory']['current_state']['truncated'] for item in memories))

    def test_task_completion_and_project_move_mark_old_and_new_projects_without_provider_calls(self):
        for action in ('completar', 'reabrir', 'proyecto'):
            old_id, new_id, sid, task_id = uuid4(), uuid4(), uuid4(), uuid4()
            task = SimpleNamespace(id=task_id, project_id=old_id, source_id=sid, status='open' if action != 'reabrir' else 'completed',
                completed_at=None, owner_text='Ana', due_at=None)
            session = MagicMock()
            session.scalar.side_effect = [SimpleNamespace(source_type='telegram_query'), None, sid, SimpleNamespace(), task]
            with patch('app.services.task_management.mark_dirty') as dirty, patch(
                'app.services.task_management.resolve_scope', return_value=Scope([new_id], 'AI Tutor')):
                edit_task(session, TaskCommand(action, task_id, 'AI Tutor'), uuid4(), settings=settings())
            self.assertEqual({call.args[1] for call in dirty.call_args_list}, {old_id, new_id} if action == 'proyecto' else {old_id})
            self.assertTrue(all(call.kwargs['event_type'] == 'task_change' for call in dirty.call_args_list))

    def test_processed_long_source_marks_project_dirty_only_after_success(self):
        from app.services.processing import process_text_source
        repo, claude = FakeSession(), FakeClaude()
        with patch('app.services.hierarchical_llm.call_json', side_effect=claude), patch(
                'app.services.hierarchical.mark_dirty') as dirty:
            process_text_source(repo, repo.source.id, settings())
        dirty.assert_called_once()
        self.assertEqual(dirty.call_args.args[1], repo.source.primary_project_id)
        self.assertEqual(dirty.call_args.kwargs['source_id'], repo.source.id)

    def test_manual_refresh_is_immediate_deterministic_and_does_not_generate_memory(self):
        self.assertEqual(parse_refresh('/refrescar@bot CIMA'), 'CIMA')
        self.assertEqual(parse_refresh('/refresh SIMA'), 'SIMA')
        self.assertIsNone(parse_refresh('/nota SIMA'))
        session, project_id = MagicMock(), uuid4()
        with patch('app.services.project_memory_events.resolve_scope', return_value=Scope([project_id], 'SIMA')), patch(
            'app.services.project_memory_events.mark_dirty') as dirty:
            answer = schedule_refresh(session, 'CIMA', settings(), 'telegram:1')
        self.assertIn('marcada', answer)
        self.assertTrue(dirty.call_args.kwargs['immediate'])
        self.assertEqual(dirty.call_args.kwargs['origin_key'], 'telegram:1')

    def test_telegram_refresh_routes_to_queue_and_never_heavy_processing(self):
        config_value = settings(TELEGRAM_BOT_TOKEN='fake', TELEGRAM_USER_ID='123')
        app.dependency_overrides[get_settings] = lambda: config_value
        app.dependency_overrides[get_session] = lambda: MagicMock()
        update = {'update_id': 1, 'message': {'message_id': 1, 'date': 1700000000,
            'from': {'id': 123, 'is_bot': False}, 'chat': {'id': 123, 'type': 'private'}, 'text': '/refrescar SIMA'}}
        with patch('app.api.routes.telegram.ingest_update', return_value={'source_id': str(uuid4())}), patch(
                'app.api.routes.telegram.schedule_refresh', return_value='Encolado') as queue, patch(
                'app.services.processing.process_source') as process:
            response = TestClient(app).post('/telegram/updates', json=update, headers={'Authorization': 'Bearer fake'})
        self.assertEqual(response.json()['answer'], 'Encolado')
        queue.assert_called_once()
        process.assert_not_called()

    def test_nightly_reconciliation_uses_lima_and_only_prior_changed_projects(self):
        item = state(change_revision=5, reconciled_revision=4, last_change_at=NOW - timedelta(days=1))
        before = datetime(2026, 10, 6, 7, 59, tzinfo=timezone.utc)
        after = before + timedelta(minutes=2)
        self.assertFalse(due_reconciliation(item, settings(), before))
        self.assertTrue(due_reconciliation(item, settings(), after))
        item.reconciled_revision = 5
        self.assertFalse(due_reconciliation(item, settings(), after))
        item.reconciled_revision = 4
        item.last_reconciliation_at = after
        self.assertFalse(due_reconciliation(item, settings(), after + timedelta(minutes=5)))
        item.last_reconciliation_at = None
        item.last_change_at = after
        self.assertFalse(due_reconciliation(item, settings(), after + timedelta(minutes=5)))

    def test_candidates_honor_debounce_and_failure_backoff_and_no_unchanged_projects(self):
        ready = state(is_dirty=True, change_revision=1, refresh_after=NOW)
        debounce = state(is_dirty=True, change_revision=1, refresh_after=NOW + timedelta(seconds=1))
        failed = state(is_dirty=True, change_revision=1, refresh_after=NOW, retry_after=NOW + timedelta(seconds=60))
        clean = state()
        session = MagicMock()
        session.scalars.return_value.all.return_value = [ready, debounce, failed, clean]
        self.assertEqual(candidates(session, settings(), NOW), [(ready.project_id, False)])
        sql = str(session.scalars.call_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn('retry_after', sql)
        self.assertIn('LIMIT', sql)

    def test_worker_failure_does_not_block_next_project_or_disclose_error(self):
        first, second = uuid4(), uuid4()
        log = io.StringIO()
        with patch('app.project_memory_worker.Session'), patch('app.project_memory_worker.candidates',
            return_value=[(first, False), (second, True)]), patch('app.project_memory_worker.refresh_project',
            side_effect=[RuntimeError('fake-secret private-body'), True]) as refresh, patch('sys.stdout', log):
            count = run_once(MagicMock(), settings(), NOW)
        self.assertEqual(count, 1)
        self.assertEqual(refresh.call_count, 2)
        self.assertNotIn('fake-secret', log.getvalue())
        self.assertNotIn('private-body', log.getvalue())

    def test_project_lock_busy_means_no_paid_call(self):
        engine = MagicMock()
        connection = MagicMock()
        engine.connect.return_value.execution_options.return_value.__enter__.return_value = connection
        connection.scalar.return_value = False
        with patch('app.services.project_memory.generate_memory') as paid:
            self.assertFalse(refresh_project(engine, uuid4(), settings(), force=True, now=NOW))
        paid.assert_not_called()
        self.assertIn('pg_try_advisory_lock', str(connection.scalar.call_args.args[0]))

    def test_invalid_timezone_is_safe_and_disabled_worker_has_no_database_access(self):
        with self.assertRaises(ValidationError) as caught:
            settings(PROJECT_MEMORY_TIMEZONE='fake-secret')
        self.assertNotIn('fake-secret', str(caught.exception))
        with patch('app.project_memory_worker.Session') as session:
            self.assertEqual(run_once(MagicMock(), settings(PROJECT_MEMORY_ENABLED=False)), 0)
        session.assert_not_called()
    def claimed_engine(self):
        engine, connection = MagicMock(), MagicMock()
        engine.connect.return_value.execution_options.return_value.__enter__.return_value = connection
        connection.scalar.return_value = True
        return engine, connection

    def test_refresh_runs_one_grounded_version_outside_source_transaction(self):
        item, source_id = state(is_dirty=True, change_revision=1, refresh_after=NOW), uuid4()
        captured = SimpleNamespace(previous_id=None, cursor=1, revision=1, version_number=1, cutoff=NOW)
        data = {'previous_memory': None, 'sources': [{'source_id': str(source_id)}], 'tasks': [], 'decisions': [], 'chunks': [], 'changes': [], 'warnings': []}
        engine, connection = self.claimed_engine()
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = item
        session.scalar.return_value = item
        with patch('app.services.project_memory.Session', return_value=session), patch(
            'app.services.project_memory.snapshot_data', return_value=(captured, data)), patch(
            'app.services.project_memory.generate_memory', return_value=memory(source_id).model_dump(mode='json')) as generate, patch(
            'app.services.project_memory.publish_memory') as publish:
            self.assertTrue(refresh_project(engine, item.project_id, settings(), now=NOW))
        generate.assert_called_once()
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[2].cursor, 1)
        self.assertIn('pg_advisory_unlock', str(connection.scalar.call_args.args[0]))

    def test_refresh_provider_failure_preserves_pointer_and_backs_off_safely(self):
        previous_id = uuid4()
        item = state(current_version_id=previous_id, is_dirty=True, change_revision=1, refresh_after=NOW)
        engine, connection = self.claimed_engine()
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = item
        session.scalar.return_value = item
        data = {'previous_memory': {}, 'sources': [{'source_id': str(uuid4())}], 'tasks': [], 'decisions': [], 'chunks': []}
        captured = SimpleNamespace(revision=1, previous_id=previous_id)
        log = io.StringIO()
        with patch('app.services.project_memory.Session', return_value=session), patch(
            'app.services.project_memory.snapshot_data', return_value=(captured, data)), patch(
            'app.services.project_memory.generate_memory', side_effect=RuntimeError('fake-secret')), patch(
            'app.services.project_memory.publish_memory') as publish, patch('sys.stdout', log):
            self.assertFalse(refresh_project(engine, item.project_id, settings(), now=NOW))
        publish.assert_not_called()
        self.assertEqual(item.current_version_id, previous_id)
        self.assertTrue(item.is_dirty)
        self.assertEqual(item.retry_after, NOW + timedelta(seconds=60))
        self.assertNotIn('fake-secret', log.getvalue())

    def test_empty_project_creates_no_artificial_memory_and_no_provider_call(self):
        item = state(is_dirty=True, change_revision=1, refresh_after=NOW)
        engine, _ = self.claimed_engine()
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = item
        session.scalar.return_value = item
        data = {'previous_memory': None, 'sources': [], 'tasks': [], 'decisions': [], 'chunks': []}
        with patch('app.services.project_memory.Session', return_value=session), patch(
            'app.services.project_memory.snapshot_data', return_value=(SimpleNamespace(revision=1, previous_id=None, cursor=1), data)), patch(
            'app.services.project_memory.generate_memory') as paid:
            self.assertFalse(refresh_project(engine, item.project_id, settings(), now=NOW))
        paid.assert_not_called()
        self.assertFalse(item.is_dirty)
        self.assertIsNone(item.current_version_id)

    def test_incremental_snapshot_includes_completed_tasks_and_manual_change_evidence(self):
        item, session, rows, _ = self.snapshot_fixture(previous=True)
        sid = rows[0][0].id
        task = SimpleNamespace(id=uuid4(), source_id=sid, title='Dashboard', status='completed', owner_text='Ana', due_at=None, completed_at=NOW)
        events = [SimpleNamespace(revision=2, created_at=NOW, source_id=sid, task_id=task.id,
            command_source_id=uuid4(), event_type='task_change', origin_key='task:' + str(uuid4()))]
        session.scalars.side_effect = [SimpleNamespace(all=lambda: events), SimpleNamespace(all=lambda: [task]), SimpleNamespace(all=lambda: [])]
        original_get = session.get.side_effect
        session.get.side_effect = lambda model, identifier: SimpleNamespace(before={'status': 'open'}, after={'status': 'completed'}, action='completar') if model is TaskChange else original_get(model, identifier)
        _, data = snapshot_data(session, item.project_id, settings())
        self.assertEqual(data['tasks'][0]['status'], 'completed')
        self.assertEqual(data['changes'][0]['after']['status'], 'completed')
        sql = str(session.scalars.call_args_list[1].args[0].compile(dialect=postgresql.dialect()))
        self.assertNotIn("tasks.status IN", sql)

    def test_reconciliation_selects_bounded_history_and_semantics_not_entire_archive(self):
        item, session, rows, _ = self.snapshot_fixture(previous=True)
        session.execute.side_effect = [SimpleNamespace(all=lambda: rows), SimpleNamespace(all=lambda: []), SimpleNamespace(all=lambda: rows)]
        cfg = settings(OPENROUTER_API_KEY='fake', OPENROUTER_EMBEDDING_MODEL='mock-model')
        with patch('app.services.project_memory.semantic_search', return_value=[]) as search:
            _, data = snapshot_data(session, item.project_id, cfg, reconciliation=True)
        self.assertEqual(search.call_args.kwargs['limit'], 8)
        self.assertIn('history_sources', data)
        self.assertIn('LIMIT', str(session.execute.call_args_list[2].args[0]))

    def test_cloud_restarts_only_memory_worker_after_exit_and_keeps_telegram(self):
        from app.cloud import supervise
        config_value = settings(TELEGRAM_BOT_TOKEN='fake', TELEGRAM_USER_ID='123', OPENROUTER_API_KEY='fake', SOURCE_PROCESSING_WORKER_ENABLED=False)
        api, bot, dead_worker, live_worker = [MagicMock() for _ in range(4)]
        for process in (api, bot, live_worker):
            process.poll.return_value = None
        dead_worker.poll.return_value = 1
        connection = MagicMock()
        connection.execution_options.return_value = connection
        connection.scalar.return_value = True
        stop = threading.Event()
        waits = []
        def wait(_):
            waits.append(1)
            if len(waits) == 4:
                stop.set()
        with patch('app.cloud.get_settings', return_value=config_value), patch('app.cloud.get_engine') as engine, patch(
                'app.cloud.subprocess.Popen', side_effect=[api, bot, dead_worker, live_worker]) as spawn, patch(
                'app.cloud.wait_api', return_value=True), patch.object(stop, 'wait', side_effect=wait), patch(
                'app.cloud.time.monotonic', side_effect=[0, 0, 0, 31]):
            engine.return_value.connect.return_value = connection
            self.assertEqual(supervise(8000, stop), 0)
        self.assertEqual(spawn.call_count, 4)
        self.assertEqual(spawn.call_args_list[2].args[0][-1], 'app.project_memory_worker')
        self.assertEqual(spawn.call_args_list[3].args[0][-1], 'app.project_memory_worker')
        bot.terminate.assert_called_once()
        live_worker.terminate.assert_called_once()

    def test_manual_cli_refresh_and_rebuild_use_same_versioned_service(self):
        from app.project_memory import main
        for command, reconciliation in (('refresh', False), ('rebuild', True)):
            with patch('sys.argv', ['app.project_memory', command, '--project', 'SIMA']), patch(
                'app.project_memory.get_settings', return_value=settings()), patch('app.project_memory.get_engine'), patch(
                'app.project_memory.Session'), patch('app.project_memory.resolve_scope', return_value=Scope([uuid4()], 'SIMA')), patch(
                'app.project_memory.schedule_refresh', return_value='Scheduled'), patch(
                'app.project_memory.refresh_project', return_value=True) as refresh:
                main()
            self.assertTrue(refresh.call_args.kwargs['force'])
            self.assertEqual(refresh.call_args.kwargs['reconciliation'], reconciliation)
    def test_budget_omits_stale_memory_if_its_conflicting_delta_cannot_fit(self):
        from app.services.retrieval_budget import bound_context, effective_limits
        plan = QueryPlan()
        pid = str(uuid4())
        context = {'scope': 'SIMA', 'warnings': [], 'tasks': [], 'decisions': [], 'chunks': [], 'recent_sources': [], 'projects': [],
            'project_memories': [{'is_dirty': True, 'project_id': pid, 'memory': {'current_state': {'text': 'Pending'}}}],
            'new_sources': [{'project_id': pid, 'source_id': str(uuid4()), 'excerpt': 'x' * 10000}]}
        result = bound_context(context, plan, effective_limits(plan), 5000)
        self.assertEqual(result['project_memories'], [])
        self.assertTrue(result['retrieval']['budget_reduced'])

    def test_short_extraction_marks_dirty_after_success_and_duplicate_has_no_new_event(self):
        from app.services.processing import process_text_source
        from app.schemas.extraction import Extraction
        from test_hierarchical import output
        repo = FakeSession(raw='SIMA. Ana sends report.')
        with patch('app.services.processing.extract', return_value=Extraction.model_validate(output(task=True))), patch(
            'app.services.processing.mark_dirty') as dirty:
            process_text_source(repo, repo.source.id, settings())
            process_text_source(repo, repo.source.id, settings())
        dirty.assert_called_once()
        self.assertEqual(dirty.call_args.args[1], repo.source.primary_project_id)
    def test_task_move_delta_does_not_import_unrelated_original_project_meeting(self):
        session, scope, _, _ = self.stale_fixture(completed=True)
        retrieve_project_memories(session, scope, settings())
        query = session.execute.call_args_list[1].args[0].compile(dialect=postgresql.dialect())
        self.assertIn('sources.primary_project_id IN', str(query))
        self.assertIn(scope.project_ids, query.params.values())
        item, session, _, _ = self.snapshot_fixture(previous=True)
        snapshot_data(session, item.project_id, settings())
        query = session.execute.call_args_list[0].args[0].compile(dialect=postgresql.dialect())
        self.assertIn('sources.primary_project_id IN', str(query))
        self.assertIn([item.project_id], query.params.values())
