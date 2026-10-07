import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings
from app.database import get_session
from app.main import app
from app.services.project_matching import match_project
from app.services.telegram_ingestion import authorized_message, ingest_update
from app.telegram import PollingError, TelegramAPI, forward_update, poll_once, process_update

# Todas las credenciales e IDs de este archivo son ficticios.
TOKEN = "test-token"
USER_ID = 12345


def update(update_id=100, content="Nota de SIMA"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 10, "date": 1700000000,
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Test"},
            "chat": {"id": USER_ID, "type": "private"},
            "text": content,
        },
    }


def project(name, *aliases):
    return SimpleNamespace(id=uuid4(), name=name, slug=name.lower().replace(" ", "-"),
                           aliases=[SimpleNamespace(alias=a) for a in aliases])


class TelegramTests(unittest.TestCase):
    def setUp(self):
        with patch.dict("os.environ", {}, clear=True):
            self.settings = Settings(_env_file=None, TELEGRAM_BOT_TOKEN=TOKEN,
                                     TELEGRAM_USER_ID=str(USER_ID))
        self.session = MagicMock()
        app.dependency_overrides[get_settings] = lambda: self.settings
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer " + TOKEN}

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_credentials_are_optional_until_telegram_is_used_and_safe(self):
        self.assertEqual(self.settings.telegram_credentials(), (TOKEN, USER_ID))
        self.assertNotIn(TOKEN, repr(self.settings))
        with patch.dict("os.environ", {}, clear=True):
            missing = Settings(_env_file=None)
            with self.assertRaisesRegex(ValueError, "Configura TELEGRAM"):
                missing.telegram_credentials()
            invalid = Settings(_env_file=None, TELEGRAM_BOT_TOKEN=TOKEN,
                               TELEGRAM_USER_ID="invalid-test-id")
            with self.assertRaises(ValueError) as caught:
                invalid.telegram_credentials()
            self.assertNotIn("invalid-test-id", str(caught.exception))

    def test_ingress_requires_authentication(self):
        with patch("app.api.routes.telegram.ingest_update") as ingest:
            self.assertEqual(self.client.post("/telegram/updates", json=update()).status_code, 401)
            self.assertEqual(self.client.post("/telegram/updates", json=update(),
                                             headers={"Authorization": "Bearer wrong"}).status_code, 401)
            ingest.assert_not_called()

    def test_ingress_disabled_without_configuration(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None)
        app.dependency_overrides[get_settings] = lambda: settings
        self.assertEqual(self.client.post("/telegram/updates", json=update(),
                                         headers=self.headers).status_code, 503)

    def test_non_owner_groups_bots_and_unsupported_updates_never_persist(self):
        payloads = []
        for path, value in (
            (("from", "id"), USER_ID + 1), (("from", "is_bot"), True),
            (("chat", "type"), "group"), (("chat", "id"), USER_ID + 1),
        ):
            candidate = update()
            candidate["message"][path[0]][path[1]] = value
            payloads.append(candidate)
        payloads.extend([{"update_id": 101, "callback_query": {}}, {"update_id": 102}])
        audio = update()
        del audio["message"]["text"]
        audio["message"]["voice"] = {"file_id": "fake-file"}
        payloads.append(audio)
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(ingest_update(self.session, payload, USER_ID), {"status": "ignored"})
        self.session.begin.assert_not_called()
        self.session.scalar.assert_not_called()

    def test_project_matching_handles_accents_boundaries_and_ambiguity(self):
        sima = project("SIMA", "SIMA 3", "SIMA3")
        inventory = project("Optimización de Inventarios", "Inventarios")
        alma = project("AI 360 Service / ALMA", "ALMA", "Alma")
        projects = [sima, inventory, alma]
        self.assertIs(match_project("Pendiente: optimizacion de inventarios.", projects), inventory)
        self.assertIs(match_project("Reunión de SIMA3", projects), sima)
        self.assertIs(match_project("Alma y ALMA", projects), alma)
        self.assertIsNone(match_project("SIMAX", projects))
        self.assertIsNone(match_project("SIMA e Inventarios", projects))
        self.assertIsNone(match_project("Algo sin nombre conocido", projects))
        another = project("Otro", "SIMA")
        self.assertIsNone(match_project("SIMA", [sima, another]))

    def test_original_text_metadata_and_project_are_saved_without_ai(self):
        selected = project("SIMA")
        source = SimpleNamespace(id=uuid4(), primary_project_id=selected.id)
        self.session.scalar.side_effect = [None, source]
        payload = update(content="  SIMA\nOriginal con tildes: reunión.  ")
        original = copy.deepcopy(payload)
        with patch("app.services.telegram_ingestion.resolve_project", return_value=selected):
            result = ingest_update(self.session, payload, USER_ID)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["project_name"], "SIMA")
        statement = self.session.scalar.call_args_list[1].args[0]
        compiled = statement.compile(dialect=postgresql.dialect())
        self.assertEqual(compiled.params["raw_content"], payload["message"]["text"])
        self.assertEqual(compiled.params["raw_metadata"], original)
        self.assertEqual(compiled.params["processing_status"], "pending")
        self.assertIn("DO NOTHING", str(compiled))
        self.assertIn("external_source = 'telegram'", str(compiled))
        self.assertEqual(payload, original)
        self.session.begin.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_unmatched_source_is_saved_with_null_project(self):
        source = SimpleNamespace(id=uuid4(), primary_project_id=None)
        self.session.scalar.side_effect = [None, source]
        with patch("app.services.telegram_ingestion.resolve_project", return_value=None):
            result = ingest_update(self.session, update(), USER_ID)
        self.assertEqual(result["status"], "saved")
        self.assertIsNone(result["project_id"])
        self.assertIsNone(result["project_name"])

    def test_retry_returns_original_source_without_rewriting(self):
        source = SimpleNamespace(id=uuid4(), primary_project_id=None)
        self.session.scalar.return_value = source
        with patch("app.services.telegram_ingestion.resolve_project") as resolve:
            result = ingest_update(self.session, update(), USER_ID)
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["source_id"], str(source.id))
        self.assertEqual(self.session.scalar.call_count, 1)
        resolve.assert_not_called()

    def test_concurrent_duplicate_uses_existing_source(self):
        source = SimpleNamespace(id=uuid4(), primary_project_id=None)
        self.session.scalar.side_effect = [None, None, source]
        with patch("app.services.telegram_ingestion.resolve_project", return_value=None):
            result = ingest_update(self.session, update(), USER_ID)
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["source_id"], str(source.id))

    def test_edited_message_is_another_source_and_keeps_the_update(self):
        payload = update(update_id=101)
        payload["edited_message"] = payload.pop("message")
        payload["edited_message"]["edit_date"] = 1700000001
        self.assertIsNotNone(authorized_message(payload, USER_ID))
        source = SimpleNamespace(id=uuid4(), primary_project_id=None)
        self.session.scalar.side_effect = [None, source]
        with patch("app.services.telegram_ingestion.resolve_project", return_value=None):
            ingest_update(self.session, payload, USER_ID)
        compiled = self.session.scalar.call_args_list[1].args[0].compile(dialect=postgresql.dialect())
        self.assertEqual(compiled.params["external_id"], "101")
        self.assertIn("edited_message", compiled.params["raw_metadata"])

    def test_http_route_returns_safe_error_on_database_failure(self):
        with patch("app.services.message_handling.ingest_update", side_effect=SQLAlchemyError("fake-secret")):
            response = self.client.post("/telegram/updates", json=update(), headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("fake-secret", response.text)

    def test_worker_forwards_to_authenticated_local_api_and_receipt(self):
        calls = []
        def transport(request):
            calls.append(request)
            return httpx.Response(200, json={"status": "saved", "source_id": str(uuid4()),
                                            "project_name": "SIMA"})
        telegram = MagicMock()
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            process_update(client, telegram, "http://127.0.0.1:8000", TOKEN, USER_ID, update())
        self.assertEqual(calls[0].headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(json.loads(calls[0].content), update())
        self.assertEqual(telegram.call.call_args.args[0], "sendMessage")
        self.assertEqual(telegram.call.call_args.args[1]["chat_id"], USER_ID)
        self.assertIn("SIMA", telegram.call.call_args.args[1]["text"])

    def test_worker_ignores_other_users_without_forward_or_reply(self):
        candidate = update()
        candidate["message"]["from"]["id"] += 1
        client, telegram = MagicMock(), MagicMock()
        process_update(client, telegram, "http://127.0.0.1:8000", TOKEN, USER_ID, candidate)
        client.post.assert_not_called()
        telegram.call.assert_not_called()

    def test_offset_only_advances_after_persistence_and_retries_duplicate(self):
        telegram = MagicMock()
        telegram.call.return_value = [update()]
        with patch("app.telegram.forward_update", side_effect=PollingError("FastAPI no confirmó")):
            with self.assertRaises(PollingError):
                poll_once(MagicMock(), telegram, "http://127.0.0.1:8000", TOKEN, USER_ID, 100)
        self.assertEqual(telegram.call.call_count, 1)
        with patch("app.telegram.forward_update", return_value={"status": "duplicate"}):
            offset = poll_once(MagicMock(), telegram, "http://127.0.0.1:8000", TOKEN, USER_ID, 100)
        self.assertEqual(offset, 101)
        self.assertEqual(telegram.call.call_count, 2)

    def test_receipt_failure_does_not_lose_persisted_source(self):
        telegram = MagicMock()
        telegram.call.side_effect = PollingError("Telegram no disponible")
        with patch("app.telegram.forward_update", return_value={
            "status": "saved", "source_id": str(uuid4()), "project_name": None,
        }), redirect_stdout(io.StringIO()) as output:
            process_update(MagicMock(), telegram, "http://127.0.0.1:8000", TOKEN, USER_ID, update())
        self.assertIn("Nota guardada", output.getvalue())

    def test_active_webhook_stops_without_changing_it(self):
        requests = []
        def transport(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"url": "https://example.test/hook"}})
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            api = TelegramAPI(client, TOKEN)
            with self.assertRaisesRegex(PollingError, "webhook activo"):
                api.ensure_no_webhook()
        self.assertEqual(len(requests), 1)
        self.assertTrue(str(requests[0].url).endswith("/getWebhookInfo"))

    def test_network_errors_do_not_expose_tokens_or_response_bodies(self):
        def transport(request):
            raise httpx.ConnectError("URL contains " + TOKEN, request=request)
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            api = TelegramAPI(client, TOKEN)
            with self.assertRaises(PollingError) as caught:
                api.call("getUpdates")
            self.assertNotIn(TOKEN, str(caught.exception))
            with self.assertRaises(PollingError) as caught:
                forward_update(client, "http://127.0.0.1:8000", TOKEN, update())
            self.assertNotIn(TOKEN, str(caught.exception))



    def test_authenticated_route_saves_original_update(self):
        source = SimpleNamespace(id=uuid4(), primary_project_id=None)
        self.session.scalar.side_effect = [None, source]
        with patch("app.services.message_handling.process_source", return_value={"source_id": str(source.id)}), patch(
            "app.services.telegram_ingestion.resolve_project") as match:
            response = self.client.post("/telegram/updates", json=update(), headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source_id"], str(source.id))
        self.assertEqual(response.json()["status"], "answered")
        match.assert_not_called()
        compiled = self.session.scalar.call_args_list[1].args[0].compile(dialect=postgresql.dialect())
        self.assertEqual(compiled.params['raw_content'], update()['message']['text'])
        self.assertEqual(compiled.params['raw_metadata']['message'], update()['message'])
        self.assertEqual(compiled.params['raw_metadata']['processing_schema'], 'action-plan-v2')

    def test_phase2_migration_only_adds_partial_unique_index(self):
        from alembic import command
        from alembic.config import Config
        buffer = io.StringIO()
        config = Config("alembic.ini", output_buffer=buffer)
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None, DATABASE_URL="postgresql://offline/db")
            with patch("app.config.get_settings", return_value=settings):
                command.upgrade(config, "0001_initial:0002_telegram_identity", sql=True)
        sql = buffer.getvalue()
        self.assertIn("CREATE UNIQUE INDEX uq_sources_telegram_external_id", sql)
        self.assertIn("external_source = 'telegram' AND external_id IS NOT NULL", sql)
        for forbidden in ("CREATE TABLE", "DROP", "TRUNCATE", "DELETE FROM"):
            self.assertNotIn(forbidden, sql)


if __name__ == "__main__":
    unittest.main()
