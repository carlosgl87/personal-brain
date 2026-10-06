import copy
import io
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql

from app.config import Settings
from app.models import (Source, SourceChunk, ProcessingRunPart, ProcessingRun, Project,
                        Task, Decision, TaskEvidence, DecisionEvidence, TaskChange, ExtractionGeneration, GenerationPart)
from app.schemas.extraction import Extraction
from app.schemas.hierarchical import ConsolidatedExtraction
from app.services.claude import ExtractionError
from app.services.hierarchical import process_hierarchical
from app.services.hierarchical_llm import (consolidation_input, validate_consolidation,
                                           validate_partial, PART_PROMPT_VERSION)
from app.services.processing import SourceNotProcessable, process_source, process_text_source


QUOTE = "Ana sends report."


def config(**values):
    with patch.dict("os.environ", {}, clear=True):
        return Settings(_env_file=None, **({"LLM_API_KEY": "fake", "LLM_MODEL": "test-model", "PROJECT_MEMORY_ENABLED": False} | values))


def output(project_id=None, task=False):
    return {"project_id": str(project_id) if project_id else None, "summary": "Meeting summary with open points.",
            "tasks": [{"title": "Send report", "description": None, "owner_text": "Ana", "due_at": None,
                       "evidence": QUOTE}] if task else [],
            "decisions": [], "people": ["Ana"] if task else [], "dates": [], "follow_ups": [], "tags": []}


class FakeSession:
    """Transactional in-memory repository; no network, dotenv or SQL execution."""
    def __init__(self, raw=None):
        self.source = SimpleNamespace(id=uuid4(), source_type="meeting_transcript",
            raw_content=raw or "SIMA. " + "x" * 5880 + " " + QUOTE + " " + "y" * 31000,
            raw_metadata={"filename": "meeting.txt"}, received_at=datetime.now(timezone.utc),
            primary_project_id=None, latest_processing_run_id=None, processing_status="pending",
            processed_at=None)
        self.chunks, self.parts, self.rows = [], {}, []
        self.projects = [SimpleNamespace(id=uuid4(), name="SIMA", slug="sima", aliases=[])]
        self.active = False
        self.edited = False
        self.commits = 0

    @contextmanager
    def begin(self):
        if self.active:
            raise AssertionError("Nested transaction")
        self.active = True
        before = (copy.deepcopy(vars(self.source)), list(self.chunks), dict(self.parts), list(self.rows))
        try:
            yield
        except BaseException:
            self.source.__dict__.update(before[0])
            self.chunks, self.parts, self.rows = before[1:]
            raise
        else:
            self.commits += 1
        finally:
            self.active = False

    def scalar(self, statement):
        assert self.active
        desc = statement.column_descriptions[0]
        model = desc["entity"]
        params = statement.compile(dialect=postgresql.dialect()).params
        if model is Source:
            return self.source.source_type if desc["name"] == "source_type" else self.source
        if model is TaskChange:
            return uuid4() if self.edited else None
        if model is SourceChunk:
            return next(c for c in self.chunks if c.id == params["id_1"])
        if model is ProcessingRunPart:
            return self.parts.get((params["source_chunk_id_1"], params["prompt_version_1"], params["model_1"]))
        if model is ExtractionGeneration:
            return next((row for row in reversed(self.rows) if isinstance(row, ExtractionGeneration)
                and row.previous_run_id == params.get("previous_run_id_1")
                and row.model == params["model_1"] and row.status in ("pending", "failed")), None)
        if model is GenerationPart:
            for row in reversed(self.rows):
                if not isinstance(row, GenerationPart):
                    continue
                if "generation_id_1" in params and row.generation_id == params["generation_id_1"] and row.source_chunk_id == params["source_chunk_id_1"]:
                    return row
                if "base_part_id_1" in params and row.base_part_id == params["base_part_id_1"] and self.get(ExtractionGeneration, row.generation_id).status == "processed":
                    return row
            return None
        raise AssertionError(model)

    def scalars(self, statement):
        assert self.active
        model = statement.column_descriptions[0]["entity"]
        params = statement.compile(dialect=postgresql.dialect()).params
        if model is SourceChunk:
            rows = sorted([c for c in self.chunks if c.chunk_version == params["chunk_version_1"]], key=lambda c: c.chunk_index)
        elif model is Task:
            rows = [row for row in self.rows if isinstance(row, Task)]
        elif model is Project:
            rows = self.projects
        else:
            raise AssertionError(model)
        return SimpleNamespace(all=lambda: rows)

    def execute(self, statement, *args):
        assert self.active
        if hasattr(statement, "column_descriptions"):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.chunks))
        params = statement.compile(dialect=postgresql.dialect()).params
        if not any(c.chunk_version == params["chunk_version"] and c.chunk_index == params["chunk_index"] for c in self.chunks):
            values = dict(params)
            values["chunk_metadata"] = values.pop("metadata")
            self.chunks.append(SourceChunk(**values))

    def add(self, row):
        assert self.active
        if isinstance(row, ProcessingRunPart):
            self.parts[(row.source_chunk_id, row.prompt_version, row.model)] = row
        else:
            self.rows.append(row)

    def flush(self):
        assert self.active

    def get(self, klass, id):
        return next(row for row in self.rows if isinstance(row, klass) and row.id == id)


class FakeClaude:
    def __init__(self, fail_part=None, fail_consolidation=False):
        self.part_calls = 0
        self.consolidation_calls = 0
        self.fail_part = fail_part
        self.fail_consolidation = fail_consolidation

    def __call__(self, settings, system, data, schema, client=None, **kwargs):
        if schema is Extraction:
            self.part_calls += 1
            if self.fail_part == self.part_calls:
                raise ExtractionError("mock failure")
            return Extraction.model_validate(output(data["project_id"], QUOTE in data["source_text"]))
        self.consolidation_calls += 1
        if self.fail_consolidation:
            raise ExtractionError("mock consolidation failure")
        payload = output(data["project_id"], bool(data["tasks"]))
        if payload["tasks"]:
            payload["tasks"][0]["candidate_ids"] = [item["candidate_id"] for item in data["tasks"]]
        payload["dispositions"] = [{"candidate_id": item["candidate_id"], "status": "merged", "final_index": 0, "reason": None}
            for kind in ("tasks", "decisions") for item in data[kind]]
        return ConsolidatedExtraction.model_validate(payload)


class HierarchicalTests(unittest.TestCase):
    def test_long_pipeline_persists_parts_then_final_rows_and_multi_chunk_evidence(self):
        session, claude = FakeSession(), FakeClaude()
        original = copy.deepcopy(vars(session.source))
        with patch("app.services.hierarchical_llm.call_json", side_effect=claude):
            response = process_text_source(session, session.source.id, config())
        self.assertGreater(len(session.chunks), 2)
        self.assertEqual(len(session.parts), len(session.chunks))
        self.assertEqual(claude.part_calls, len(session.chunks))
        self.assertEqual(claude.consolidation_calls, 1)
        self.assertEqual(response["tasks_count"], 1)
        tasks = [row for row in session.rows if isinstance(row, Task)]
        evidence = [row for row in session.rows if isinstance(row, TaskEvidence)]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(evidence), 2)
        for row in evidence:
            self.assertEqual(session.source.raw_content[row.char_start:row.char_end], row.evidence)
            self.assertEqual(row.task_id, tasks[0].id)
        self.assertEqual(session.source.raw_content, original["raw_content"])
        self.assertEqual(session.source.raw_metadata, original["raw_metadata"])
        self.assertEqual(session.source.processing_status, "processed")
        run = next(row for row in session.rows if isinstance(row, ProcessingRun))
        self.assertEqual(run.result["extraction_mode"], "hierarchical")
        self.assertEqual(len(run.result["part_ids"]), len(session.parts))

    def test_retry_after_part_failure_keeps_successes_and_only_calls_pending(self):
        session, claude = FakeSession(), FakeClaude(fail_part=3)
        with patch("app.services.hierarchical_llm.call_json", side_effect=claude):
            with self.assertRaises(ExtractionError):
                process_text_source(session, session.source.id, config())
        self.assertEqual(len(session.parts), 2)
        self.assertEqual(session.rows, [])
        self.assertEqual(session.source.processing_status, "failed")
        claude.fail_part = None
        before = claude.part_calls
        with patch("app.services.hierarchical_llm.call_json", side_effect=claude):
            process_text_source(session, session.source.id, config())
        self.assertEqual(claude.part_calls - before, len(session.chunks) - 2)
        self.assertEqual(claude.consolidation_calls, 1)

    def test_consolidation_failure_keeps_all_parts_and_retry_calls_only_consolidation(self):
        session, claude = FakeSession(), FakeClaude(fail_consolidation=True)
        with patch("app.services.hierarchical_llm.call_json", side_effect=claude):
            with self.assertRaises(ExtractionError):
                process_text_source(session, session.source.id, config())
        self.assertEqual(session.rows, [])
        self.assertEqual(len(session.parts), len(session.chunks))
        previous_calls = claude.part_calls
        claude.fail_consolidation = False
        with patch("app.services.hierarchical_llm.call_json", side_effect=claude):
            process_text_source(session, session.source.id, config())
        self.assertEqual(claude.part_calls, previous_calls)
        self.assertEqual(claude.consolidation_calls, 2)

    def test_successful_retry_never_calls_claude_or_duplicates_final_rows(self):
        session, claude = FakeSession(), FakeClaude()
        with patch("app.services.hierarchical_llm.call_json", side_effect=claude):
            process_text_source(session, session.source.id, config())
        before = len(session.rows)
        with patch("app.services.hierarchical_llm.call_json") as paid:
            response = process_text_source(session, session.source.id, config())
        paid.assert_not_called()
        self.assertEqual(response["status"], "already_processed")
        self.assertEqual(len(session.rows), before)

    def test_changed_model_does_not_reuse_incompatible_partials(self):
        session, claude = FakeSession(), FakeClaude()
        with patch("app.services.hierarchical_llm.call_json", side_effect=claude):
            process_text_source(session, session.source.id, config())
        before = claude.part_calls
        with patch("app.services.hierarchical_llm.call_json", side_effect=claude):
            process_text_source(session, session.source.id, config(LLM_MODEL="another-model"), force=True)
        self.assertEqual(claude.part_calls - before, len(session.chunks))
        self.assertEqual(len(session.parts), len(session.chunks) * 2)

    def test_short_source_keeps_direct_extraction_without_partial_calls(self):
        session = FakeSession(raw="SIMA. " + QUOTE)
        with patch("app.services.processing.extract", return_value=Extraction.model_validate(output(task=True))) as direct, patch(
                "app.services.hierarchical.extract_part") as part:
            process_text_source(session, session.source.id, config())
        direct.assert_called_once()
        part.assert_not_called()
        self.assertEqual(session.chunks, [])

    def test_disabled_or_too_many_chunks_never_calls_claude_and_preserves_original(self):
        for values in ({"HIERARCHICAL_EXTRACTION_ENABLED": False}, {"HIERARCHICAL_MAX_CHUNKS": 1}):
            session = FakeSession()
            original = session.source.raw_content
            with patch("app.services.hierarchical_llm.call_json") as paid:
                with self.assertRaises(SourceNotProcessable):
                    process_text_source(session, session.source.id, config(**values))
            paid.assert_not_called()
            self.assertEqual(session.source.raw_content, original)
            if values.get("HIERARCHICAL_MAX_CHUNKS"):
                self.assertGreater(len(session.chunks), 1)

    def test_part_evidence_and_global_offsets_are_exact(self):
        session = FakeSession(raw="prefix " + QUOTE + " suffix")
        chunk = SimpleNamespace(id=uuid4(), content=QUOTE, char_start=7, char_end=7 + len(QUOTE))
        result = validate_partial(Extraction.model_validate(output(task=True)), session.source, chunk)
        location = result["evidence"]["tasks"][0][0]
        self.assertEqual(location["char_start"], 7)
        self.assertEqual(location["char_end"], 7 + len(QUOTE))
        bad = output(task=True)
        bad["tasks"][0]["evidence"] = "invented quote"
        with self.assertRaises(ExtractionError):
            validate_partial(Extraction.model_validate(bad), session.source, chunk)

    def test_partial_cannot_assign_a_project(self):
        session = FakeSession(raw=QUOTE)
        chunk = SimpleNamespace(id=uuid4(), content=QUOTE, char_start=0, char_end=len(QUOTE))
        with self.assertRaises(ExtractionError):
            validate_partial(Extraction.model_validate(output(uuid4(), task=True)), session.source, chunk)

    def test_ambiguous_final_project_remains_null(self):
        session, claude = FakeSession(), FakeClaude()
        session.source.raw_content += " AI Tutor"
        session.projects.append(SimpleNamespace(id=uuid4(), name="AI Tutor", slug="ai-tutor", aliases=[]))
        original_call = claude.__call__
        def proposed(*args, **kwargs):
            result = original_call(*args, **kwargs)
            if isinstance(result, ConsolidatedExtraction):
                result.project_id = session.projects[0].id
            return result
        with patch("app.services.hierarchical_llm.call_json", side_effect=proposed):
            process_text_source(session, session.source.id, config())
        self.assertIsNone(session.source.primary_project_id)
        self.assertTrue(all(row.project_id is None for row in session.rows if isinstance(row, Task)))

    def test_reprocess_failure_preserves_previous_success_and_manual_edits_block_reprocess(self):
        session, claude = FakeSession(), FakeClaude()
        with patch("app.services.hierarchical_llm.call_json", side_effect=claude):
            process_text_source(session, session.source.id, config())
        previous_id = session.source.latest_processing_run_id
        claude.fail_consolidation = True
        with patch("app.services.hierarchical_llm.call_json", side_effect=claude):
            with self.assertRaises(ExtractionError):
                process_text_source(session, session.source.id, config(), force=True)
        self.assertEqual(session.source.latest_processing_run_id, previous_id)
        self.assertEqual(session.source.processing_status, "processed")
        session.edited = True
        with patch("app.services.hierarchical_llm.call_json") as paid:
            with self.assertRaises(SourceNotProcessable):
                process_text_source(session, session.source.id, config(), force=True)
        paid.assert_not_called()

    def test_consolidation_budget_failure_happens_after_parts_are_committed(self):
        session, claude = FakeSession(), FakeClaude()
        session.source.raw_content += "z" * 60000
        with patch("app.services.hierarchical_llm.call_json", side_effect=claude):
            with self.assertRaises(ExtractionError):
                process_text_source(session, session.source.id, config(HIERARCHICAL_CONSOLIDATION_MAX_ITEMS=10))
        self.assertEqual(len(session.parts), len(session.chunks))
        self.assertEqual(session.rows, [])
        self.assertEqual(claude.consolidation_calls, 0)

    def test_long_audio_transcript_uses_identical_pipeline(self):
        session = FakeSession()
        session.source.source_type = "audio_transcript"
        with patch("app.services.hierarchical_llm.call_json", side_effect=FakeClaude()):
            result = process_source(session, session.source.id, config())
        self.assertEqual(result["status"], "processed")
        self.assertEqual(len(session.parts), len(session.chunks))

    def test_audio_wrapper_transcribes_then_selects_long_text_pipeline(self):
        session = MagicMock()
        audio_id, transcript_id = uuid4(), uuid4()
        session.scalar.side_effect = ["telegram_audio", "SIMA"]
        with patch("app.services.processing.transcribe_audio", return_value=transcript_id), patch(
                "app.services.processing.process_text_source", return_value={"status": "processed"}) as text:
            result = process_source(session, audio_id, config())
        text.assert_called_once_with(session, transcript_id, unittest.mock.ANY, force=False)
        self.assertEqual(result["transcript_source_id"], str(transcript_id))

    def test_ingest_process_option_reuses_process_source_and_keeps_commit_before_extraction(self):
        from app.ingest import main
        source_id = uuid4()
        with patch("sys.argv", ["app.ingest", "--file", "meeting.txt", "--type", "meeting_transcript", "--process"]), patch(
                "app.ingest.get_settings", return_value=config()), patch("app.ingest.get_engine"), patch(
                "app.ingest.Session"), patch("app.ingest.ingest_file", return_value=source_id), patch(
                "app.ingest.index_source", return_value={"embedded": 0, "pending_embeddings": 0}), patch(
                "app.ingest.process_source", return_value={"status": "processed"}) as process:
            main()
        self.assertEqual(process.call_args.args[1], source_id)

    def candidate_fixture(self, kind="tasks"):
        session = FakeSession(raw=QUOTE)
        chunk = SimpleNamespace(id=uuid4(), content=QUOTE, char_start=0, char_end=len(QUOTE))
        payload = output(task=kind == "tasks")
        if kind == "decisions":
            payload["decisions"] = [{"decision_text": "Use PostgreSQL", "decided_at": None, "evidence": QUOTE}]
        saved = validate_partial(Extraction.model_validate(payload), session.source, chunk)
        part = SimpleNamespace(id=uuid4(), source_chunk_id=chunk.id, part_index=0, result=saved)
        data, candidates = consolidation_input(session.source, [part], session.projects, config())
        final = dict(payload)
        final[kind] = [dict(item, candidate_ids=[data[kind][index]["candidate_id"]])
                       for index, item in enumerate(payload[kind])]
        final["dispositions"] = [{"candidate_id": item["candidate_id"], "status": "kept", "final_index": index, "reason": None}
            for index, item in enumerate(data[kind])]
        return session, data, candidates, final

    def test_consolidation_rejects_unknown_references_owner_date_and_evidence(self):
        for field, value in (("owner_text", "invented owner"), ("due_at", "2026-10-05T10:00:00-05:00"),
                             ("evidence", "invented quote"), ("candidate_ids", ["unknown"])):
            session, data, candidates, final = self.candidate_fixture()
            final["tasks"][0][field] = value
            with self.assertRaises(ExtractionError):
                validate_consolidation(ConsolidatedExtraction.model_validate(final), candidates,
                                       session.source, session.projects)

    def test_consolidation_rejects_missing_candidates(self):
        session, data, candidates, final = self.candidate_fixture()
        final["tasks"] = []
        with self.assertRaises(ExtractionError):
            validate_consolidation(ConsolidatedExtraction.model_validate(final), candidates,
                                   session.source, session.projects)

    def test_distinct_occurrences_are_not_merged_just_for_identical_titles(self):
        session, data, candidates, final = self.candidate_fixture()
        first_id = next(iter(candidates["tasks"]))
        second_id = "tasks:second:0"
        second = copy.deepcopy(candidates["tasks"][first_id])
        second["candidate_id"] = second_id
        second["provenance"][0]["char_start"] = 100
        second["provenance"][0]["char_end"] = 100 + len(QUOTE)
        candidates["tasks"][second_id] = second
        final["tasks"].append(dict(final["tasks"][0], candidate_ids=[second_id]))
        final["dispositions"].append({"candidate_id": second_id, "status": "kept", "final_index": 1, "reason": None})
        result, provenance = validate_consolidation(ConsolidatedExtraction.model_validate(final),
                                                   candidates, session.source, session.projects)
        self.assertEqual(len(result.tasks), 2)
        self.assertEqual(len(provenance["tasks"]), 2)

    def test_decision_consolidation_keeps_grounded_provenance(self):
        session, data, candidates, final = self.candidate_fixture("decisions")
        result, provenance = validate_consolidation(ConsolidatedExtraction.model_validate(final),
                                                   candidates, session.source, session.projects)
        self.assertEqual(len(result.decisions), 1)
        self.assertEqual(provenance["decisions"][0][0]["evidence"], QUOTE)
        self.assertIn("processing_run_part_id", provenance["decisions"][0][0])

    def test_final_decisions_persist_evidence_rows(self):
        session, base = FakeSession(), FakeClaude()
        def with_decision(settings, system, data, schema, client=None, **kwargs):
            if schema is Extraction:
                result = base(settings, system, data, schema, client, **kwargs)
                if QUOTE in data["source_text"]:
                    payload = result.model_dump(mode="json")
                    payload["decisions"] = [{"decision_text": "Use PostgreSQL", "decided_at": None, "evidence": QUOTE}]
                    result = Extraction.model_validate(payload)
                return result
            result = base(settings, system, data, schema, client, **kwargs)
            payload = result.model_dump(mode="json")
            if data["decisions"]:
                payload["decisions"] = [{"decision_text": "Use PostgreSQL", "decided_at": None, "evidence": QUOTE,
                    "candidate_ids": [item["candidate_id"] for item in data["decisions"]]}]
            return ConsolidatedExtraction.model_validate(payload)
        with patch("app.services.hierarchical_llm.call_json", side_effect=with_decision):
            result = process_text_source(session, session.source.id, config())
        self.assertEqual(result["decisions_count"], 1)
        self.assertEqual(len([row for row in session.rows if isinstance(row, DecisionEvidence)]), 2)

    def test_additive_migration_new_tables_checks_and_immutable_history(self):
        buffer = io.StringIO()
        with patch("app.config.get_settings", return_value=config(DATABASE_URL="postgresql://offline/db")):
            command.upgrade(Config("alembic.ini", output_buffer=buffer), "0006_memory_reasoning:head", sql=True)
        sql = buffer.getvalue()
        for table in ("processing_run_parts", "task_evidence", "decision_evidence"):
            self.assertIn("CREATE TABLE " + table, sql)
        self.assertIn("source_id, source_chunk_id, chunk_version, prompt_version, model", sql)
        self.assertIn("char_end > char_start", sql)
        for forbidden in ("DROP ", "TRUNCATE ", "DELETE FROM ", "UPDATE sources", "ALTER TABLE sources"):
            self.assertNotIn(forbidden, sql)


if __name__ == "__main__":
    unittest.main()
