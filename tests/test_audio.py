
import hashlib
import io
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.config import Settings, get_settings
from app.database import get_session
from app.main import app
from app.models import AudioAsset, Source
from app.services.audio import (
    AudioError, AudioTooLarge, authorized_audio, download_audio, ingest_audio,
)
from app.services.processing import process_source
from app.services.queries import current_source
from app.services.transcription import TranscriptionError, transcribe_audio, transcribe_bytes
from app.telegram import process_update


def update(user_id=12345, kind="voice"):
    return {"update_id": 101, "message": {
        "message_id": 11, "date": 1700000000,
        "from": {"id": user_id, "is_bot": False},
        "chat": {"id": user_id, "type": "private"},
        kind: {"file_id": "fake-file-id", "file_unique_id": "fake-unique",
               "duration": 5, "file_size": 5, "mime_type": "audio/ogg"},
    }}


def source():
    return SimpleNamespace(
        id=uuid4(), source_type="telegram_audio", raw_content="", raw_metadata=update(),
        primary_project_id=None, latest_transcript_source_id=None, latest_processing_run_id=None,
        received_at=datetime.now(timezone.utc), processing_status="awaiting_download",
    )


class AudioTests(unittest.TestCase):
    def setUp(self):
        with patch.dict("os.environ", {}, clear=True):
            self.settings = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="fake-token",
                                     TELEGRAM_USER_ID="12345", LLM_API_KEY="fake-key",
                                     LLM_MODEL="fake-model", OPENROUTER_API_KEY="fake-openrouter-key")

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_voice_and_audio_only_from_owner_private_chat(self):
        self.assertIsNotNone(authorized_audio(update(), 12345))
        self.assertIsNotNone(authorized_audio(update(kind="audio"), 12345))
        self.assertIsNone(authorized_audio(update(999), 12345))
        group = update()
        group["message"]["chat"]["type"] = "group"
        self.assertIsNone(authorized_audio(group, 12345))
        malformed = update()
        del malformed["message"]["voice"]["file_unique_id"]
        self.assertIsNone(authorized_audio(malformed, 12345))

    def test_unauthorized_audio_never_downloads_or_persists(self):
        session = MagicMock()
        with patch("app.services.audio.download_audio") as download:
            result = ingest_audio(session, update(999), 12345, self.settings)
        self.assertEqual(result, {"status": "ignored"})
        session.begin.assert_not_called()
        download.assert_not_called()

    def test_download_uses_fixed_telegram_origin_and_preserves_exact_bytes(self):
        calls = []
        def transport(request):
            calls.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/file_1.oga", "file_size": 5}})
            return httpx.Response(200, content=b"audio")
        media = update()["message"]["voice"]
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            data = download_audio("fake-token", media, 100, client)
        self.assertEqual(data, b"audio")
        self.assertEqual(calls[1].url.host, "api.telegram.org")

    def test_download_rejects_path_traversal_and_oversized_stream(self):
        media = update()["message"]["voice"]
        for path, body, limit, error in (
            ("../secret", b"audio", 100, AudioError),
            ("https://evil.test/file", b"audio", 100, AudioError),
            ("voice/file.oga", b"audio-more", 6, AudioTooLarge),
        ):
            def transport(request):
                if request.method == "POST":
                    return httpx.Response(200, json={"ok": True, "result": {"file_path": path}})
                return httpx.Response(200, content=body)
            with httpx.Client(transport=httpx.MockTransport(transport)) as client:
                with self.assertRaises(error):
                    download_audio("fake-token", media, limit, client)

    def test_network_exception_does_not_show_token(self):
        def transport(request):
            raise httpx.ConnectError("fake-token in URL", request=request)
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            with self.assertRaises(AudioError) as caught:
                download_audio("fake-token", update()["message"]["voice"], 100, client)
        self.assertNotIn("fake-token", str(caught.exception))

    def test_capture_stores_blob_hash_and_original_metadata_before_transcription(self):
        audio = source()
        session = MagicMock()
        session.scalar.side_effect = [None, audio, audio, None]
        with patch("app.services.audio.download_audio", return_value=b"audio"):
            result = ingest_audio(session, update(), 12345, self.settings)
        asset = session.add.call_args.args[0]
        self.assertIsInstance(asset, AudioAsset)
        self.assertEqual(asset.content, b"audio")
        self.assertEqual(asset.sha256, hashlib.sha256(b"audio").hexdigest())
        self.assertEqual(asset.source_id, audio.id)
        self.assertEqual(result["media_type"], "audio")
        self.assertEqual(audio.processing_status, "pending_transcription")
        statement = session.scalar.call_args_list[1].args[0].compile(dialect=postgresql.dialect())
        self.assertEqual(statement.params["raw_content"], "")
        self.assertEqual(statement.params["raw_metadata"], update())
        self.assertIn("DO NOTHING", str(statement))

    def test_duplicate_capture_reuses_original_asset_without_downloading(self):
        audio = source()
        session = MagicMock()
        session.scalar.side_effect = [audio, audio, SimpleNamespace(content=b"audio")]
        with patch("app.services.audio.download_audio") as download:
            result = ingest_audio(session, update(), 12345, self.settings)
        self.assertEqual(result["status"], "duplicate")
        download.assert_not_called()
        session.add.assert_not_called()

    def test_download_retry_reports_newly_saved_asset(self):
        audio = source()
        session = MagicMock()
        session.scalar.side_effect = [audio, audio, None]
        with patch("app.services.audio.download_audio", return_value=b"audio"):
            result = ingest_audio(session, update(), 12345, self.settings)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(session.add.call_count, 1)

    def test_rejected_audio_keeps_reference_and_reports_no_file_saved(self):
        audio = source()
        payload = update()
        payload["message"]["voice"]["duration"] = 601
        session = MagicMock()
        session.scalar.side_effect = [None, audio, audio, None]
        with patch("app.services.audio.download_audio") as download:
            result = ingest_audio(session, payload, 12345, self.settings)
        self.assertEqual(result["status"], "answered")
        self.assertIn("pero no el archivo", result["answer"])
        self.assertEqual(audio.processing_status, "audio_rejected")
        session.add.assert_not_called()
        download.assert_not_called()

    def test_transcript_is_new_source_linked_to_immutable_audio(self):
        audio = source()
        asset = SimpleNamespace(content=b"audio", sha256=hashlib.sha256(b"audio").hexdigest(), mime_type="audio/ogg")
        session = MagicMock()
        session.scalar.side_effect = [audio, asset]
        session.scalars.return_value = []
        with patch("app.services.transcription.transcribe_bytes", return_value="Texto transcrito"), patch(
            "app.services.transcription.resolve_project", return_value=None,
        ):
            transcript_id = transcribe_audio(session, audio.id, self.settings)
        child = session.add.call_args.args[0]
        self.assertIsInstance(child, Source)
        self.assertEqual(child.id, transcript_id)
        self.assertEqual(child.parent_source_id, audio.id)
        self.assertEqual(child.raw_content, "Texto transcrito")
        self.assertEqual(child.source_type, "audio_transcript")
        self.assertEqual(child.raw_metadata["transcription"]["provider"], "openrouter")
        self.assertEqual(child.raw_metadata["transcription"]["model"], "openai/whisper-large-v3-turbo")
        self.assertEqual(child.raw_metadata["transcription"]["audio_sha256"], asset.sha256)
        self.assertEqual(audio.raw_content, "")
        self.assertEqual(audio.raw_metadata, update())
        self.assertEqual(audio.latest_transcript_source_id, child.id)
        self.assertEqual(audio.processing_status, "transcribed")

    def test_repeat_transcription_reuses_same_source_without_model_call(self):
        audio = source()
        audio.latest_transcript_source_id = uuid4()
        session = MagicMock()
        session.scalar.return_value = audio
        with patch("app.services.transcription.transcribe_bytes") as model:
            result = transcribe_audio(session, audio.id, self.settings)
        self.assertEqual(result, audio.latest_transcript_source_id)
        model.assert_not_called()
        session.add.assert_not_called()

    def test_integrity_failure_does_not_transcribe_or_modify_original(self):
        audio = source()
        session = MagicMock()
        session.scalar.side_effect = [audio, SimpleNamespace(content=b"modified", sha256="wrong"), audio]
        with patch("app.services.transcription.transcribe_bytes") as model:
            with self.assertRaises(TranscriptionError):
                transcribe_audio(session, audio.id, self.settings)
        self.assertEqual(audio.processing_status, "transcription_failed")
        self.assertEqual(audio.raw_content, "")
        model.assert_not_called()
        session.add.assert_not_called()

    def test_audio_processor_runs_existing_pipeline_on_transcript_only(self):
        session = MagicMock()
        session.scalar.return_value = "telegram_audio"
        transcript_id = uuid4()
        with patch("app.services.processing.transcribe_audio", return_value=transcript_id), patch(
            "app.services.processing.process_text_source", return_value={"status": "processed"},
        ) as processor:
            result = process_source(session, uuid4(), self.settings)
        self.assertEqual(processor.call_args.args[1], transcript_id)
        self.assertEqual(result["transcript_source_id"], str(transcript_id))

    def test_route_authorization_precedes_audio_download(self):
        app.dependency_overrides[get_settings] = lambda: self.settings
        app.dependency_overrides[get_session] = lambda: MagicMock()
        client = TestClient(app)
        with patch("app.api.routes.telegram.ingest_audio") as capture:
            self.assertEqual(client.post("/telegram/updates", json=update()).status_code, 401)
            response = client.post("/telegram/updates", json=update(999), headers={"Authorization": "Bearer fake-token"})
        self.assertEqual(response.json(), {"status": "ignored"})
        capture.assert_not_called()

    def test_audio_download_failure_returns_safe_retryable_error(self):
        app.dependency_overrides[get_settings] = lambda: self.settings
        app.dependency_overrides[get_session] = lambda: MagicMock()
        with patch("app.api.routes.telegram.ingest_audio", side_effect=AudioError("fake-token")):
            response = TestClient(app).post("/telegram/updates", json=update(),
                                           headers={"Authorization": "Bearer fake-token"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("fake-token", response.text)

    def test_worker_accepts_owner_voice_and_reports_transcript_reference(self):
        telegram = MagicMock()
        transcript_id = str(uuid4())
        with patch("app.telegram.forward_update", return_value={
            "status": "saved", "source_id": str(uuid4()), "media_type": "audio", "project_name": None,
        }), patch("app.telegram.process_saved_source", return_value={
            "status": "processed", "transcript_source_id": transcript_id,
            "tasks_count": 1, "decisions_count": 0, "summary": "Resumen",
        }):
            process_update(MagicMock(), telegram, "http://127.0.0.1:8000", "fake-token", 12345, update(), processing=True)
        message = telegram.call.call_args.args[1]["text"]
        self.assertIn("Audio original guardado", message)
        self.assertIn(transcript_id, message)
        self.assertIn("Tareas: 1", message)

    def test_queries_only_show_current_transcript_not_duplicate_audio_parent(self):
        compiled = select(Source.id).where(current_source()).compile(dialect=postgresql.dialect())
        sql = str(compiled)
        self.assertIn("sources.latest_transcript_source_id IS NULL", sql)
        self.assertIn("audio_parent.latest_transcript_source_id = sources.id", sql)
        self.assertIn("audio_transcript", str(compiled.params))

    def test_audio_migration_adds_storage_links_and_immutability_without_deletion(self):
        buffer = io.StringIO()
        config = Config("alembic.ini", output_buffer=buffer)
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None, DATABASE_URL="postgresql://offline/db")
            with patch("app.config.get_settings", return_value=settings):
                command.upgrade(config, "0003_processing_runs:head", sql=True)
        sql = buffer.getvalue()
        self.assertIn("CREATE TABLE audio_assets", sql)
        self.assertIn("ADD COLUMN parent_source_id", sql)
        self.assertIn("CREATE TRIGGER preserve_original_audio", sql)
        self.assertIn("BYTEA", sql)
        for prohibited in ("DROP TABLE", "TRUNCATE", "DELETE FROM", "UPDATE sources"):
            self.assertNotIn(prohibited, sql)


if __name__ == "__main__":
    unittest.main()
