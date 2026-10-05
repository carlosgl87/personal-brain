
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.config import Settings, get_settings
from app.database import get_session
from app.main import app
from app.models import Source, Task
from app.services.queries import (
    Query, Scope, answer_chunks, answer_query, current_source,
    decision_statement, parse_query, resolve_scope, source_statement, task_statement,
)
from app.services.processing import SourceNotProcessable, process_source
from app.services.telegram_ingestion import ingest_update
from app.telegram import PollingError, process_update


def telegram_update(text, user_id=12345):
    return {"update_id": 100, "message": {
        "message_id": 10, "date": 1700000000,
        "from": {"id": user_id, "is_bot": False},
        "chat": {"id": user_id, "type": "private"}, "text": text,
    }}


def project(name="SIMA", aliases=(), company_id=None, area_id=None):
    return SimpleNamespace(id=uuid4(), name=name, slug=name.lower(),
                           aliases=[SimpleNamespace(alias=a) for a in aliases],
                           company_id=company_id, area_id=area_id, status="active")


class QueryTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.clear()

    def test_commands_and_natural_language(self):
        cases = {
            "¿Qué tengo pendiente de Catusita?": Query("tasks", "catusita"),
            "¿Qué decidimos sobre SIMA?": Query("decisions", "sima"),
            "¿Cuál es el estado actual del proyecto SIMA?": Query("status", "sima"),
            "¿Qué pasó en las últimas reuniones de SIMA?": Query("summary", "sima"),
            "/pendientes": Query("tasks"),
            "/pendientes con Ana": Query("tasks", owner="ana"),
            "/pendientes SIMA con Ana": Query("tasks", "sima", "ana"),
            "/pendientes@brain_bot SIMA3": Query("tasks", "sima3"),
            "/decisiones SIMA": Query("decisions", "sima"),
            "/resumen SIMA": Query("summary", "sima"),
            "/estado SIMA": Query("status", "sima"),
            "/ayuda": Query("help"),
            "¿Una pregunta desconocida?": Query("help"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_query(text), expected)
        self.assertIsNone(parse_query("SIMA: Ana enviará el informe."))
        self.assertIsNone(parse_query("/nota ¿Qué decidimos sobre SIMA?"))

    def test_exact_alias_company_and_area_resolution_without_fuzzy_matching(self):
        company = SimpleNamespace(id=uuid4(), name="Catusita", slug="catusita")
        area = SimpleNamespace(id=uuid4(), name="Consultora", slug="consultora")
        p = project(aliases=["SIMA 3", "SIMA3"], company_id=company.id, area_id=area.id)
        for name in ("sima3", "catusita", "consultora", "empresa catusita"):
            session = MagicMock()
            session.scalars.return_value.all.side_effect = [[p], [company], [area]]
            self.assertEqual(resolve_scope(session, name).project_ids, [p.id])
        session = MagicMock()
        session.scalars.return_value.all.side_effect = [[p], [company], [area]]
        self.assertIsNotNone(resolve_scope(session, "sim").error)

    def test_ambiguous_name_requires_clarification(self):
        first, second = project("One", ["shared"]), project("Two", ["shared"])
        session = MagicMock()
        session.scalars.return_value.all.side_effect = [[first, second], [], []]
        result = resolve_scope(session, "shared")
        self.assertIn("ambiguo", result.error)
        self.assertEqual(result.project_ids, [])
        session.execute.assert_not_called()

    def test_company_scope_includes_all_its_projects_only(self):
        company = SimpleNamespace(id=uuid4(), name="Catusita", slug="catusita")
        one = project("One", company_id=company.id)
        two = project("Two", company_id=company.id)
        unrelated = project("Other", company_id=uuid4())
        session = MagicMock()
        session.scalars.return_value.all.side_effect = [[one, two, unrelated], [company], []]
        self.assertEqual(resolve_scope(session, "catusita").project_ids, [one.id, two.id])

    def test_sql_filters_current_runs_open_tasks_and_telegram_edits(self):
        scope = Scope([uuid4()], "Test")
        sql = str(task_statement(scope).compile(dialect=postgresql.dialect()))
        self.assertIn("tasks.processing_run_id = sources.latest_processing_run_id", sql)
        self.assertIn("tasks.processing_run_id IS NULL", sql)
        self.assertIn("tasks.completed_at IS NULL", sql)
        self.assertIn("tasks.status IN", sql)
        self.assertIn("EXISTS", sql)
        self.assertIn("CAST", sql)
        self.assertIn("tasks.project_id IN", sql)
        decisions = str(decision_statement(scope).compile(dialect=postgresql.dialect()))
        self.assertIn("decisions.processing_run_id = sources.latest_processing_run_id", decisions)
        sources = str(source_statement(scope).compile(dialect=postgresql.dialect()))
        self.assertIn("sources.source_type !=", sources)
        self.assertIn("sources.latest_processing_run_id", sources)

    def test_old_edits_only_hidden_after_new_success_and_same_chat_message(self):
        compiled = select(Source.id).where(current_source()).compile(dialect=postgresql.dialect())
        sql = str(compiled)
        self.assertIn("newer_source.latest_processing_run_id IS NOT NULL", sql)
        params = list(compiled.params.values())
        self.assertIn("message_id", params)
        self.assertIn("chat", params)
        self.assertIn("edited_message", params)
        self.assertIn("edit_date", params)

    def test_sql_never_interpolates_user_input(self):
        session = MagicMock()
        session.scalars.return_value.all.side_effect = [[], [], []]
        result = resolve_scope(session, "sima drop table tasks")
        self.assertIsNotNone(result.error)
        session.execute.assert_not_called()

    def test_tasks_answer_has_owner_local_date_and_source_reference(self):
        source_id = uuid4()
        task = SimpleNamespace(
            id=uuid4(), title="Enviar informe", owner_text="Ana", source_id=source_id,
            due_at=datetime(2026, 10, 9, 22, tzinfo=timezone.utc),
        )
        session = MagicMock()
        session.execute.return_value.all.return_value = [(task, "SIMA")]
        with patch("app.services.queries.resolve_scope", return_value=Scope([uuid4()], "Proyecto: SIMA")):
            answer = answer_query(session, Query("tasks", "sima"))
        self.assertIn("Enviar informe", answer)
        self.assertIn("Ana", answer)
        self.assertIn("09/10/2026 17:00 Lima", answer)
        self.assertIn(str(source_id), answer)
        session.add.assert_not_called()

    def test_owner_filter_matches_normalized_full_names(self):
        session = MagicMock()
        session.scalars.return_value.all.return_value = ["Ana Pérez", "Ana", None]
        session.execute.return_value.all.return_value = []
        answer = answer_query(session, Query("tasks", owner="ana perez"))
        sql = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
        self.assertIn(["Ana Pérez"], sql.params.values())
        self.assertIn("No hay pendientes", answer)

    def test_empty_decisions_and_summary_do_not_invent_information(self):
        session = MagicMock()
        session.execute.return_value.all.return_value = []
        scope = Scope([uuid4()], "Proyecto: SIMA")
        with patch("app.services.queries.resolve_scope", return_value=scope):
            self.assertIn("No hay decisiones", answer_query(session, Query("decisions", "sima")))
            self.assertIn("No hay notas", answer_query(session, Query("summary", "sima")))

    def test_summary_uses_latest_saved_result_and_reports_unprocessed_sources(self):
        source = SimpleNamespace(id=uuid4(), received_at=datetime.now(timezone.utc))
        run = SimpleNamespace(result={"summary": "Decidimos usar PostgreSQL"})
        session = MagicMock()
        session.execute.return_value.all.return_value = [(source, run), (source, None)]
        with patch("app.services.queries.resolve_scope", return_value=Scope([uuid4()], "SIMA")):
            answer = answer_query(session, Query("summary", "sima"))
        self.assertIn("Decidimos usar PostgreSQL", answer)
        self.assertIn("Sin extracción disponible", answer)
        self.assertIn(str(source.id), answer)

    def test_status_is_catalog_data_not_generated_project_progress(self):
        p = project()
        session = MagicMock()
        session.scalars.return_value.all.return_value = [p]
        session.scalar.side_effect = [1, 2]
        with patch("app.services.queries.resolve_scope", return_value=Scope([p.id], "SIMA")):
            answer = answer_query(session, Query("status", "sima"))
        self.assertIn("Pendientes registrados: 1", answer)
        self.assertIn("Decisiones registradas: 2", answer)
        self.assertIn("estado de catálogo = active", answer)
        self.assertIn("no es una evaluación", answer)

    def test_query_is_saved_verbatim_but_skipped_by_extraction(self):
        session = MagicMock()
        source = SimpleNamespace(id=uuid4(), primary_project_id=None)
        session.scalar.side_effect = [None, source]
        update = telegram_update("¿Qué tengo pendiente de SIMA?")
        with patch("app.services.telegram_ingestion.resolve_project") as resolver:
            ingest_update(session, update, 12345, is_query=True)
        params = session.scalar.call_args_list[1].args[0].compile(dialect=postgresql.dialect()).params
        self.assertEqual(params["source_type"], "telegram_query")
        self.assertEqual(params["processing_status"], "skipped")
        self.assertEqual(params["raw_content"], update["message"]["text"])
        self.assertEqual(params["raw_metadata"], update)
        resolver.assert_not_called()

    def test_processing_query_is_rejected_even_with_force(self):
        session = MagicMock()
        session.scalar.return_value = SimpleNamespace(source_type="telegram_query")
        settings = MagicMock()
        with patch("app.services.processing.extract") as llm:
            with self.assertRaises(SourceNotProcessable):
                process_source(session, uuid4(), settings, force=True)
        llm.assert_not_called()
        session.add.assert_not_called()

    def test_route_answers_and_reuses_source_on_retry(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="fake-token", TELEGRAM_USER_ID="12345")
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_session] = lambda: MagicMock()
        source_id = str(uuid4())
        client = TestClient(app)
        update = telegram_update("¿Qué tengo pendiente de SIMA?")
        with patch("app.api.routes.telegram.ingest_update", return_value={
            "status": "duplicate", "source_id": source_id,
        }) as ingest, patch("app.api.routes.telegram.answer_query", return_value="Enviar informe"):
            response = client.post("/telegram/updates", json=update, headers={"Authorization": "Bearer fake-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "answered")
        self.assertEqual(response.json()["source_id"], source_id)
        self.assertEqual(response.json()["answer"], "Enviar informe")
        self.assertTrue(ingest.call_args.kwargs["is_query"])

    def test_unauthorized_user_gets_no_query_answer(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="fake-token", TELEGRAM_USER_ID="12345")
        session = MagicMock()
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_session] = lambda: session
        with patch("app.api.routes.telegram.answer_query") as answer:
            response = TestClient(app).post("/telegram/updates", json=telegram_update("/pendientes", 999),
                                           headers={"Authorization": "Bearer fake-token"})
        self.assertEqual(response.json(), {"status": "ignored"})
        answer.assert_not_called()
        session.scalar.assert_not_called()

    def test_worker_query_response_does_not_call_claude_even_when_enabled(self):
        telegram = MagicMock()
        answer = "Pendientes: Enviar informe."
        with patch("app.telegram.forward_update", return_value={
            "status": "answered", "source_id": str(uuid4()), "answer": answer,
        }), patch("app.telegram.process_saved_source") as processor:
            process_update(MagicMock(), telegram, "http://127.0.0.1:8000", "fake-token", 12345,
                           telegram_update("/pendientes SIMA"), processing=True)
        processor.assert_not_called()
        self.assertEqual(telegram.call.call_args.args[1]["text"], answer)

    def test_long_answers_are_split_without_loss_including_unicode(self):
        answer = "Tarea 😀\n" * 1000
        chunks = answer_chunks(answer)
        self.assertEqual("".join(chunks), answer)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 3500 for chunk in chunks))

    def test_query_reply_failure_does_not_silently_acknowledge_update(self):
        telegram = MagicMock()
        telegram.call.side_effect = PollingError("safe")
        with patch("app.telegram.forward_update", return_value={
            "status": "answered", "source_id": str(uuid4()), "answer": "Tarea",
        }):
            with self.assertRaises(PollingError):
                process_update(MagicMock(), telegram, "http://127.0.0.1:8000", "fake-token", 12345,
                               telegram_update("/pendientes"))


if __name__ == "__main__":
    unittest.main()
