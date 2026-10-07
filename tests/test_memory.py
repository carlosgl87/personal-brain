import hashlib
import io
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql

from app.config import Settings
from app.services.chunking import chunk_text, chunk_source, chunk_version
from app.services.embeddings import EmbeddingError, embed_texts
from app.services.file_ingestion import ingest_file, read_text_file
from app.services.memory import (MemoryError, backfill_candidates, filtered_scope,
                                index_source, semantic_search, semantic_statement)
from app.services.processing import SourceNotProcessable, process_text_source
from app.services.queries import Scope


def settings(**values):
    with patch.dict("os.environ", {}, clear=True):
        return Settings(_env_file=None, **({"PROJECT_MEMORY_ENABLED": False} | values))


def embedding_settings():
    return settings(OPENROUTER_API_KEY="fake-secret", OPENROUTER_EMBEDDING_MODEL="test/model",
                    OPENROUTER_EMBEDDING_DIMENSIONS=3)


class MemoryTests(unittest.TestCase):
    def test_chunking_covers_full_original_deterministically(self):
        original = ("Paragraph one. Sentence two.\n\n" * 600) + "final"
        chunks = chunk_text(original)
        self.assertGreater(len(chunks), 2)
        self.assertEqual(chunks, chunk_text(original))
        recovered, end = "", 0
        for chunk in chunks:
            self.assertLessEqual(len(chunk.content), 6000)
            self.assertEqual(chunk.content, original[chunk.start:chunk.end])
            self.assertLessEqual(chunk.start, end)
            recovered += original[end:chunk.end]
            end = chunk.end
        self.assertEqual(recovered, original)
        self.assertTrue(any(c.content.endswith("\n\n") for c in chunks[:-1]))

    def test_chunking_empty_unbroken_and_unicode(self):
        self.assertEqual(chunk_text("  \n"), [])
        self.assertEqual(len(chunk_text("x" * 14000)), 3)
        text = "\u00bfReuni\u00f3n? \U0001f600 " * 1200
        for chunk in chunk_text(text):
            self.assertEqual(chunk.content, text[chunk.start:chunk.end])
        with self.assertRaises(ValueError):
            chunk_text("text", size=10)

    def test_source_types_exclude_questions_and_original_audio(self):
        for kind in ("telegram_query", "telegram_audio"):
            self.assertEqual(chunk_source(SimpleNamespace(source_type=kind, raw_content="text"), settings()), [])
        self.assertTrue(chunk_source(SimpleNamespace(source_type="audio_transcript", raw_content="text"), settings()))

    def test_logical_version_changes_only_with_chunking_configuration(self):
        baseline = chunk_version(settings())
        for values in ({"MEMORY_CHUNK_VERSION": "v2"}, {"MEMORY_CHUNK_SIZE": 5000}, {"MEMORY_CHUNK_OVERLAP": 250}):
            self.assertNotEqual(baseline, chunk_version(settings(**values)))

        for values in ({"OPENROUTER_EMBEDDING_MODEL": "other"}, {"OPENROUTER_EMBEDDING_DIMENSIONS": 3}):
            self.assertEqual(baseline, chunk_version(settings(**values)))

    def test_mock_embeddings_restore_order_and_use_fixed_endpoint(self):
        client = MagicMock()
        client.post.return_value.json.return_value = {"data": [
            {"index": 1, "embedding": [0, 1, 0]}, {"index": 0, "embedding": [1, 0, 0]}]}
        self.assertEqual(embed_texts(["one", "two"], embedding_settings(), client), [[1, 0, 0], [0, 1, 0]])
        self.assertEqual(client.post.call_args.args[0], "https://openrouter.ai/api/v1/embeddings")
        self.assertEqual(client.post.call_args.kwargs["json"]["model"], "test/model")

    def test_embedding_response_rejects_bad_dimensions_nan_zero_and_cardinality(self):
        for data in ([{"index": 0, "embedding": [1]}], [{"index": 0, "embedding": [0, 0, 0]}],
                     [{"index": 0, "embedding": [1, float("nan"), 0]}], [],
                     [{"index": 2, "embedding": [1, 0, 0]}]):
            client = MagicMock()
            client.post.return_value.json.return_value = {"data": data}
            with self.assertRaises(EmbeddingError):
                embed_texts(["text"], embedding_settings(), client)

    def test_embedding_errors_do_not_echo_provider_secrets(self):
        client = MagicMock()
        client.post.side_effect = RuntimeError("fake-secret private-body")
        with self.assertRaises(EmbeddingError) as caught:
            embed_texts(["text"], embedding_settings(), client)
        self.assertNotIn("fake-secret", str(caught.exception))
        with self.assertRaises(EmbeddingError):
            embed_texts(["text"], settings(), client)

    def test_indexing_keeps_chunks_committed_before_embedding_failure(self):
        session = MagicMock()
        source_id, chunk_id = uuid4(), uuid4()
        source = SimpleNamespace(source_type="manual_note", raw_content="complete original")
        chunk = SimpleNamespace(id=chunk_id, content=source.raw_content, embedding=None)
        session.scalar.side_effect = [source, chunk, None]
        session.scalars.return_value = [chunk_id]
        with patch("app.services.memory.embed_texts", side_effect=EmbeddingError("failure")):
            with self.assertRaises(EmbeddingError):
                index_source(session, source_id, embedding_settings())
        sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("ON CONFLICT (source_id, chunk_version, chunk_index) DO NOTHING", sql)
        self.assertEqual(session.begin.return_value.__exit__.call_count, 3)
        self.assertIsNone(session.begin.return_value.__exit__.call_args_list[0].args[0])
        self.assertEqual(source.raw_content, "complete original")

    def test_indexing_retry_skips_completed_vectors(self):
        session = MagicMock()
        source_id = uuid4()
        session.scalar.return_value = SimpleNamespace(source_type="manual_note", raw_content="original")
        session.scalars.return_value = []
        with patch("app.services.memory.embed_texts") as embed:
            result = index_source(session, source_id, embedding_settings())
        embed.assert_not_called()
        self.assertEqual(result["embedded"], 0)

    def test_indexing_records_model_on_success_and_skips_concurrent_completion(self):
        session = MagicMock()
        first = SimpleNamespace(id=uuid4(), content="a", embedding=None)
        done = SimpleNamespace(id=uuid4(), content="b", embedding=[1, 0, 0])
        session.scalar.side_effect = [SimpleNamespace(source_type="manual_note", raw_content="original"), first, None, done, uuid4()]
        session.scalars.return_value = [uuid4(), uuid4()]
        with patch("app.services.memory.embed_texts", return_value=[[1, 0, 0]]) as embed:
            result = index_source(session, uuid4(), embedding_settings())
        self.assertEqual(embed.call_count, 1)
        saved = session.add.call_args.args[0]
        self.assertEqual(saved.model, "test/model")
        self.assertEqual(saved.dimensions, 3)
        self.assertIsNone(first.embedding)
        self.assertEqual(list(done.embedding), [1, 0, 0])
        self.assertEqual(result["embedded"], 1)

    def test_search_sql_filters_current_sources_version_model_dimensions_and_scope(self):
        now, project_id = datetime.now(timezone.utc), uuid4()
        statement = semantic_statement([1, 0, 0], embedding_settings(), [project_id], "manual_note", now, now)
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        for value in ("<=>", "chunk_embeddings.model =", "chunk_embeddings.dimensions =", "logical_version =",
                      "primary_project_id IN", "received_at >=", "received_at <=", "NOT (EXISTS"):
            self.assertIn(value, sql)
        self.assertIn("test/model", compiled.params.values())
        self.assertIn(3, compiled.params.values())

    def test_search_returns_provenance_and_distance(self):
        session = MagicMock()
        chunk = SimpleNamespace(id=uuid4(), content="evidence", char_start=0, char_end=8,
                                chunk_version="v1", embedding_model="test/model")
        source = SimpleNamespace(id=uuid4(), primary_project_id=uuid4(), source_type="manual_note",
                                 received_at=datetime.now(timezone.utc))
        session.execute.return_value.all.return_value = [(chunk, source, 0.2)]
        with patch("app.services.memory.embed_texts", return_value=[[1, 0, 0]]):
            result = semantic_search(session, "evidence", embedding_settings())[0]
        self.assertEqual(result["source_id"], str(source.id))
        self.assertEqual(result["chunk_id"], str(chunk.id))
        self.assertEqual(result["project_id"], str(source.primary_project_id))
        self.assertEqual(result["score"], 0.8)
        self.assertEqual(result["received_at"], source.received_at.isoformat())

    def test_empty_scope_never_calls_embeddings_and_unknown_scope_never_widens(self):
        with patch("app.services.memory.embed_texts") as embed:
            self.assertEqual(semantic_search(MagicMock(), "question", embedding_settings(), project_ids=[]), [])
        embed.assert_not_called()
        with patch("app.services.memory.resolve_scope", return_value=Scope([], "", "Unknown")):
            with self.assertRaises(MemoryError):
                filtered_scope(MagicMock(), project="missing")

    def test_scope_intersection(self):
        p1, p2 = uuid4(), uuid4()
        with patch("app.services.memory.resolve_scope", side_effect=[Scope([p1, p2], ""), Scope([p2], "")]):
            self.assertEqual(filtered_scope(MagicMock(), company="company", area="area"), {p2})

    def test_backfill_is_bounded_and_retries_missing_vectors(self):
        session = MagicMock()
        session.scalars.return_value = []
        backfill_candidates(session, embedding_settings(), 10)
        sql = str(session.scalars.call_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("NOT (EXISTS (SELECT chunk_embeddings.id", sql)
        self.assertIn("LIMIT", sql)
        for limit in (0, 101):
            with self.assertRaises(MemoryError):
                backfill_candidates(session, embedding_settings(), limit)

    def test_long_file_preserves_exact_utf8_and_metadata_without_absolute_path(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "meeting.md"
            original = ("\u00bfReuni\u00f3n?\r\n\r\n" * 10000).encode()
            path.write_bytes(original)
            content, metadata = read_text_file(path)
            self.assertEqual(content.encode(), original)
            self.assertEqual(metadata["sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(metadata["file_size"], len(original))
            self.assertEqual(metadata["filename"], "meeting.md")
            self.assertNotIn(directory, str(metadata))
            self.assertGreater(len(chunk_text(content)), 2)

    def test_file_ingestion_unresolved_project_is_null_and_duplicate_is_reused(self):
        session = MagicMock()
        session.scalar.return_value = None
        metadata = {"filename": "meeting.txt", "sha256": "a" * 64, "file_size": 8,
                    "ingest_method": "text-file-cli"}
        with patch("app.services.file_ingestion.read_text_file", return_value=("original", metadata)), patch(
                "app.services.file_ingestion.resolve_scope", return_value=Scope([], "", "Ambiguous")):
            source_id = ingest_file(session, "ignored.txt", project_name="ambiguous")
            source = session.add.call_args.args[0]
            self.assertEqual(source.id, source_id)
            self.assertIsNone(source.primary_project_id)
            self.assertEqual(source.raw_content, "original")
            self.assertEqual(source.raw_metadata, metadata | {"processing_schema": "action-plan-v2"})
            session.reset_mock()
            session.scalar.return_value = SimpleNamespace(id=source_id)
            self.assertEqual(ingest_file(session, "ignored.txt"), source_id)
            session.add.assert_not_called()

    def test_binary_and_unsupported_files_are_rejected(self):
        with TemporaryDirectory() as directory:
            for name, raw in (("file.pdf", b"text"), ("file.txt", b"\x00binary"),
                              ("file.md", b"\xff"), ("empty.txt", b" ")):
                path = Path(directory) / name
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    read_text_file(path)

    def test_long_extraction_is_explicitly_blocked_before_claude(self):
        session = MagicMock()
        session.scalar.return_value = SimpleNamespace(id=uuid4(), source_type="meeting_transcript",
            raw_content="x" * 30001, latest_processing_run_id=None)
        config = settings(LLM_API_KEY="fake", LLM_MODEL="test", HIERARCHICAL_EXTRACTION_ENABLED=False)
        with patch("app.services.processing.extract") as extract:
            with self.assertRaises(SourceNotProcessable):
                process_text_source(session, uuid4(), config)
        extract.assert_not_called()
        session.add.assert_not_called()

    def test_additive_migration_compiles_without_database_or_real_env(self):
        buffer = io.StringIO()
        config = Config("alembic.ini", output_buffer=buffer)
        with patch("app.config.get_settings", return_value=settings(DATABASE_URL="postgresql://offline/db")):
            command.upgrade(config, "0005_task_changes:0011_task_completion_attempts", sql=True)
        sql = buffer.getvalue()
        for expected in ("CREATE EXTENSION IF NOT EXISTS vector", "CREATE TABLE source_chunks",
                         "CREATE TABLE reasoning_runs", "UNIQUE (source_id, chunk_version, chunk_index)"):
            self.assertIn(expected, sql)
        for forbidden in ("DROP ", "TRUNCATE ", "DELETE FROM ", "UPDATE sources"):
            self.assertNotIn(forbidden, sql)


if __name__ == "__main__":
    unittest.main()
