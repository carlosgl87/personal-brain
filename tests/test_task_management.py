import io
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.database import get_session
from app.main import app
from app.models import TaskChange
from app.services.processing import SourceNotProcessable, process_text_source
from app.services.queries import Scope
from app.services.task_management import TaskCommand, edit_task, parse_task_command


class TaskManagementTests(unittest.TestCase):
    def setUp(self):
        self.task = SimpleNamespace(id=uuid4(), source_id=uuid4(), project_id=None,
            status="open", completed_at=None, due_at=None, owner_text="Ana")
        self.command_id = uuid4()
        self.session = MagicMock()
        self.session.scalar.side_effect = [
            SimpleNamespace(source_type="telegram_query"), None, self.task.source_id,
            SimpleNamespace(), self.task,
        ]

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_parser_requires_exact_command_uuid_and_arguments(self):
        for name in ("completar", "reabrir", "fecha", "responsable", "proyecto"):
            self.assertIsNotNone(parse_task_command("/" + name + " bad").error)
        self.assertIsNone(parse_task_command("/nota /completar bad"))
        self.assertIsNone(parse_task_command("Completar tarea"))
        self.assertIsNotNone(parse_task_command("/completar " + str(self.task.id) + " extra").error)
        result = parse_task_command("/responsable@bot " + str(self.task.id) + " Ana Gomez")
        self.assertEqual(result.value, "Ana Gomez")
        self.assertEqual(result.task_id, self.task.id)

    def test_completion_records_before_after_and_cannot_touch_original(self):
        edit_task(self.session, TaskCommand("completar", self.task.id), self.command_id)
        event = self.session.add.call_args.args[0]
        self.assertIsInstance(event, TaskChange)
        self.assertEqual(event.before["status"], "open")
        self.assertEqual(event.after["status"], "completed")
        self.assertIsNotNone(self.task.completed_at.tzinfo)
        self.assertEqual(event.command_source_id, self.command_id)

    def test_duplicate_update_returns_cached_answer_without_edits(self):
        self.session.scalar.side_effect = [SimpleNamespace(source_type="telegram_query"),
                                           SimpleNamespace(answer="Previous answer")]
        answer = edit_task(self.session, TaskCommand("completar", self.task.id), self.command_id)
        self.assertEqual(answer, "Previous answer")
        self.assertEqual(self.task.status, "open")
        self.session.add.assert_not_called()

    def test_reopen_clears_completion(self):
        self.task.status = "completed"
        self.task.completed_at = datetime.now(timezone.utc)
        edit_task(self.session, TaskCommand("reabrir", self.task.id), self.command_id)
        self.assertEqual(self.task.status, "open")
        self.assertIsNone(self.task.completed_at)

    def test_date_is_explicit_lima_and_invalid_date_writes_nothing(self):
        edit_task(self.session, TaskCommand("fecha", self.task.id, "2026-10-09 17:00"), self.command_id)
        self.assertEqual(self.task.due_at.utcoffset().total_seconds(), -18000)
        for value in ("mañana", "2026-02-30 17:00", "2026-10-09"):
            self.setUp()
            answer = edit_task(self.session, TaskCommand("fecha", self.task.id, value), self.command_id)
            self.assertIsNone(self.task.due_at)
            self.session.add.assert_not_called()
            self.assertIn("fecha", answer)

    def test_clear_date_and_owner(self):
        for action, value, field in (("fecha", "sin fecha", "due_at"),
                                     ("responsable", "sin asignar", "owner_text")):
            self.setUp()
            edit_task(self.session, TaskCommand(action, self.task.id, value), self.command_id)
            self.assertIsNone(getattr(self.task, field))

    def test_owner_preserves_name_and_limits_length(self):
        edit_task(self.session, TaskCommand("responsable", self.task.id, "Ana Gomez"), self.command_id)
        self.assertEqual(self.task.owner_text, "Ana Gomez")
        self.setUp()
        edit_task(self.session, TaskCommand("responsable", self.task.id, "x" * 251), self.command_id)
        self.session.add.assert_not_called()

    def test_project_requires_exact_unambiguous_existing_project(self):
        project_id = uuid4()
        with patch("app.services.task_management.resolve_scope", return_value=Scope([project_id], "SIMA")):
            edit_task(self.session, TaskCommand("proyecto", self.task.id, "CIMA"), self.command_id)
        self.assertEqual(self.task.project_id, project_id)
        self.setUp()
        with patch("app.services.task_management.resolve_scope", return_value=Scope([], "", "Ambiguo")):
            self.assertEqual(edit_task(self.session, TaskCommand("proyecto", self.task.id, "x"),
                                       self.command_id), "Ambiguo")
        self.session.add.assert_not_called()

    def test_historical_or_missing_task_is_not_changed(self):
        self.session.scalar.side_effect = [SimpleNamespace(source_type="telegram_query"), None, None, None]
        self.assertIn("vigente", edit_task(self.session, TaskCommand("completar", uuid4()), self.command_id))
        self.session.add.assert_not_called()

    def test_reprocessing_manual_edits_is_blocked_before_llm(self):
        session = MagicMock()
        session.scalar.side_effect = [SimpleNamespace(source_type="telegram_text", id=uuid4(),
                    latest_processing_run_id=uuid4()), uuid4()]
        with patch("app.services.processing.extract") as extract:
            with self.assertRaises(SourceNotProcessable):
                process_text_source(session, uuid4(), MagicMock(), force=True)
        extract.assert_not_called()
        session.add.assert_not_called()

    def test_route_checks_owner_before_edit_and_routes_command_without_extraction(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="fake", TELEGRAM_USER_ID="123")
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_session] = lambda: MagicMock()
        update = {"update_id": 1, "message": {"message_id": 1, "date": 1700000000,
            "from": {"id": 123, "is_bot": False}, "chat": {"id": 123, "type": "private"},
            "text": "/completar " + str(self.task.id)}}
        client = TestClient(app)
        with patch("app.api.routes.telegram.edit_task", return_value="Updated") as edit, patch(
            "app.api.routes.telegram.ingest_update", return_value={"source_id": str(self.command_id)}) as ingest:
            self.assertEqual(client.post("/telegram/updates", json=update).status_code, 401)
            edit.assert_not_called()
            response = client.post("/telegram/updates", json=update, headers={"Authorization": "Bearer fake"})
            self.assertEqual(response.json()["answer"], "Updated")
            self.assertTrue(ingest.call_args.kwargs["is_query"])
            update["message"]["from"]["id"] = 999
            edit.reset_mock()
            from app.services.telegram_ingestion import ingest_update
            ingest.side_effect = ingest_update
            self.assertEqual(client.post("/telegram/updates", json=update,
                headers={"Authorization": "Bearer fake"}).json(), {"status": "ignored"})
            edit.assert_not_called()

    def test_migration_adds_immutable_history_without_modifying_existing_rows(self):
        output = io.StringIO()
        config = Config("alembic.ini", output_buffer=output)
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None, DATABASE_URL="postgresql://offline/db")
            with patch("app.config.get_settings", return_value=settings):
                alembic_command.upgrade(config, "0004_audio_sources:head", sql=True)
        sql = output.getvalue()
        self.assertIn("CREATE TABLE task_changes", sql)
        self.assertIn("CREATE TRIGGER preserve_task_change", sql)
        self.assertIn("UNIQUE (command_source_id)", sql)
        for forbidden in ("DROP TABLE", "DELETE FROM", "TRUNCATE", "UPDATE tasks"):
            self.assertNotIn(forbidden, sql)


if __name__ == "__main__":
    unittest.main()
