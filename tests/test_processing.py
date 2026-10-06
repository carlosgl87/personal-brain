
import io
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.database import get_session
from app.main import app
from app.models import Task, Decision, ProcessingRun
from app.schemas.extraction import Extraction
from app.services.claude import ExtractionError, extract
from app.services.processing import SourceNotFound, process_source
from app.telegram import process_update

def output(project_id=None):
    return {
        "project_id": str(project_id) if project_id else None, "summary": "Revisión de SIMA.",
        "tasks": [{"title": "Enviar informe", "description": None, "owner_text": "Ana",
                   "due_at": "2026-10-09T17:00:00-05:00", "evidence": "Ana enviará el informe"}],
        "decisions": [{"decision_text": "Usar PostgreSQL", "decided_at": None,
                       "evidence": "Decidimos usar PostgreSQL"}],
        "people": ["Ana"], "dates": ["9 de octubre"], "follow_ups": [], "tags": ["SIMA"],
    }


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        with patch.dict("os.environ", {}, clear=True):
            self.settings = Settings(_env_file=None, PROJECT_MEMORY_ENABLED=False, LLM_API_KEY="fake-test-key",
                                     LLM_MODEL="test-model", TELEGRAM_BOT_TOKEN="fake-bot",
                                     TELEGRAM_USER_ID="12345")
        self.source = SimpleNamespace(
            id=uuid4(), source_type="telegram_text", raw_content="SIMA: Ana enviará el informe el 9 de octubre. Decidimos usar PostgreSQL.",
            raw_metadata={"message": {"date": 1700000000}},
            received_at=datetime.now(timezone.utc), primary_project_id=None,
            latest_processing_run_id=None, processing_status="pending", processed_at=None,
        )
        self.project = SimpleNamespace(id=uuid4(), name="SIMA", slug="sima", aliases=[],
                                       area=SimpleNamespace(name="Consultora"), company=None)
        self.session = MagicMock()
        self.session.scalar.side_effect = lambda statement: None if "task_changes" in str(statement) else self.source
        self.session.scalars.return_value.all.return_value = [self.project]

    def tearDown(self):
        app.dependency_overrides.clear()

    def provider_call(self, result=None, stop="end_turn", status=200):
        captured = []
        def transport(request):
            captured.append(request)
            return httpx.Response(status, json={
                "stop_reason": stop,
                "content": [{"type": "text", "text": json.dumps(result or output())}],
            })
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            response = extract(self.settings, self.source, [self.project], client)
        return response, captured

    def test_provider_uses_structured_output_without_secrets_in_prompt(self):
        result, requests = self.provider_call()
        self.assertEqual(result.tasks[0].title, "Enviar informe")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("fake-test-key", requests[0].content.decode())
        self.assertEqual(requests[0].headers["x-api-key"], "fake-test-key")
        self.assertEqual(payload["model"], "test-model")
        self.assertIn(self.source.raw_content, json.loads(payload["messages"][0]["content"])["source_text"])

    def test_provider_rejects_unknown_project_and_ungrounded_evidence(self):
        bad_project = output(uuid4())
        bad_evidence = output()
        bad_evidence["tasks"][0]["evidence"] = "Texto inventado"
        for data in (bad_project, bad_evidence):
            with self.subTest(data=data):
                with self.assertRaises(ExtractionError):
                    self.provider_call(data)

    def test_truncation_refusal_and_http_errors_are_safe(self):
        for stop, status in (("max_tokens", 200), ("refusal", 200), ("end_turn", 401), ("end_turn", 429)):
            with self.subTest(stop=stop, status=status):
                with self.assertRaises(ExtractionError) as caught:
                    self.provider_call(stop=stop, status=status)
                self.assertNotIn("fake-test-key", str(caught.exception))

    def test_invalid_dates_and_extra_fields_are_rejected(self):
        data = output()
        data["tasks"][0]["due_at"] = "2026-10-09T17:00:00"
        with self.assertRaises(ValueError):
            Extraction.model_validate(data)
        data = output()
        data["unexpected"] = "value"
        with self.assertRaises(ValueError):
            Extraction.model_validate(data)

    def test_success_persists_history_tasks_decisions_and_keeps_original(self):
        raw = self.source.raw_content
        metadata = self.source.raw_metadata.copy()
        result = Extraction.model_validate(output(self.project.id))
        with patch("app.services.processing.extract", return_value=result) as llm:
            response = process_source(self.session, self.source.id, self.settings)
        added = [call.args[0] for call in self.session.add.call_args_list]
        run = next(x for x in added if isinstance(x, ProcessingRun))
        task = next(x for x in added if isinstance(x, Task))
        decision = next(x for x in added if isinstance(x, Decision))
        self.assertEqual(task.processing_run_id, run.id)
        self.assertEqual(decision.processing_run_id, run.id)
        self.assertEqual(task.project_id, self.project.id)
        self.assertEqual(run.result["people"], ["Ana"])
        self.assertEqual(self.source.latest_processing_run_id, run.id)
        self.assertEqual(self.source.processing_status, "processed")
        self.assertIsNotNone(self.source.processed_at.tzinfo)
        self.assertEqual(self.source.raw_content, raw)
        self.assertEqual(self.source.raw_metadata, metadata)
        self.assertEqual(response["tasks_count"], 1)
        self.assertEqual(response["decisions_count"], 1)
        llm.assert_called_once()
        query = self.session.scalar.call_args.args[0]
        self.assertIsNotNone(query._for_update_arg)

    def test_repeat_skips_llm_and_does_not_duplicate_derived_rows(self):
        run = SimpleNamespace(id=uuid4(), source_id=self.source.id, result=output())
        self.source.latest_processing_run_id = run.id
        self.session.get.return_value = run
        with patch("app.services.processing.extract") as llm:
            response = process_source(self.session, self.source.id, self.settings)
        self.assertEqual(response["status"], "already_processed")
        self.session.add.assert_not_called()
        llm.assert_not_called()

    def test_reprocess_adds_a_new_run_without_deleting_history(self):
        previous_id = uuid4()
        self.source.latest_processing_run_id = previous_id
        with patch("app.services.processing.extract", return_value=Extraction.model_validate(output())):
            response = process_source(self.session, self.source.id, self.settings, force=True)
        self.assertNotEqual(response["run_id"], str(previous_id))
        self.session.delete.assert_not_called()
        self.assertEqual(self.session.add.call_count, 3)

    def test_provider_failure_marks_failed_without_derived_rows(self):
        raw = self.source.raw_content
        with patch("app.services.processing.extract", side_effect=ExtractionError("safe error")):
            with self.assertRaises(ExtractionError):
                process_source(self.session, self.source.id, self.settings)
        self.assertEqual(self.source.processing_status, "failed")
        self.assertEqual(self.source.raw_content, raw)
        self.assertIsNone(self.source.processed_at)
        self.session.add.assert_not_called()

    def test_failed_reprocess_preserves_previous_success(self):
        previous_id = uuid4()
        self.source.latest_processing_run_id = previous_id
        self.source.processing_status = "processed"
        with patch("app.services.processing.extract", side_effect=ExtractionError("safe")):
            with self.assertRaises(ExtractionError):
                process_source(self.session, self.source.id, self.settings, force=True)
        self.assertEqual(self.source.latest_processing_run_id, previous_id)
        self.assertEqual(self.source.processing_status, "processed")

    def test_ambiguous_source_remains_without_project(self):
        self.source.raw_content += " También AI Tutor."
        other = SimpleNamespace(id=uuid4(), name="AI Tutor", slug="ai-tutor", aliases=[])
        self.session.scalars.return_value.all.return_value = [self.project, other]
        with patch("app.services.processing.extract", return_value=Extraction.model_validate(output(self.project.id))):
            process_source(self.session, self.source.id, self.settings)
        self.assertIsNone(self.source.primary_project_id)
        for call in self.session.add.call_args_list:
            if isinstance(call.args[0], (Task, Decision)):
                self.assertIsNone(call.args[0].project_id)

    def test_missing_source_is_404_without_provider_call(self):
        self.session.scalar.side_effect = None
        self.session.scalar.return_value = None
        with patch("app.services.processing.extract") as llm:
            with self.assertRaises(SourceNotFound):
                process_source(self.session, uuid4(), self.settings)
            llm.assert_not_called()

    def test_processing_route_requires_auth_and_returns_safe_failure(self):
        app.dependency_overrides[get_settings] = lambda: self.settings
        app.dependency_overrides[get_session] = lambda: self.session
        client = TestClient(app)
        path = "/sources/" + str(self.source.id) + "/process"
        self.assertEqual(client.post(path).status_code, 401)
        with patch("app.api.routes.processing.process_source", side_effect=ExtractionError("fake-test-key")):
            response = client.post(path, headers={"Authorization": "Bearer fake-bot"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("fake-test-key", response.text)

    def test_worker_option_processes_after_save_and_includes_counts(self):
        update = {
            "update_id": 1, "message": {"message_id": 1, "date": 1700000000,
            "from": {"id": 12345, "is_bot": False}, "chat": {"id": 12345, "type": "private"},
            "text": self.source.raw_content},
        }
        telegram = MagicMock()
        with patch("app.telegram.forward_update", return_value={
            "status": "saved", "source_id": str(self.source.id), "project_name": "SIMA",
        }), patch("app.telegram.process_saved_source", return_value={
            "status": "processed", "summary": "Resumen", "tasks_count": 1, "decisions_count": 1,
        }) as processor:
            process_update(MagicMock(), telegram, "http://127.0.0.1:8000", "fake-bot", 12345,
                           update, processing=True)
        processor.assert_called_once()
        self.assertIn("Tareas: 1 | Decisiones: 1", telegram.call.call_args.args[1]["text"])

    def test_migration_is_additive_and_keeps_existing_tables(self):
        buffer = io.StringIO()
        config = Config("alembic.ini", output_buffer=buffer)
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None, DATABASE_URL="postgresql://offline/db")
            with patch("app.config.get_settings", return_value=settings):
                command.upgrade(config, "0002_telegram_identity:head", sql=True)
        sql = buffer.getvalue()
        self.assertIn("CREATE TABLE processing_runs", sql)
        self.assertIn("ADD COLUMN latest_processing_run_id", sql)
        self.assertIn("ADD COLUMN processing_run_id", sql)
        for prohibited in ("DROP TABLE", "TRUNCATE", "DELETE FROM", "UPDATE sources"):
            self.assertNotIn(prohibited, sql)


if __name__ == "__main__":
    unittest.main()
