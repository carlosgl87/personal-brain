"""Regression coverage for failures found in the production audit; no paid calls."""
import copy
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import UniqueConstraint
from app.config import get_settings
from app.database import get_session
from app.main import app
from app.models import ProjectMemoryEvent
from app.schemas.project_memory import ProjectMemoryContent
from app.services.reasoning_llm import provider_schema
from app.services.reasoning import reasoning_question
from app.services.queries import parse_query, Scope
from app.services.project_memory import refresh_project
from app.services.project_memory_retrieval import retrieve_project_memories
from app.services.memory import ensure_source_chunks
from app.services.chunking import chunk_version
from app.services.embeddings import embed_texts, EmbeddingError
from app.services.telegram_ingestion import authorized_message
from app.telegram import TelegramAPI, PollingError, RetryablePollingError, run
from app.cloud import stop_child
from test_project_memory import settings, state, NOW
import test_project_memory as memory_fixtures
from test_hierarchical import FakeSession, config
from test_telegram import update, USER_ID


class ProductionReviewTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.clear()

    def test_event_unique_constraints_have_distinct_names(self):
        constraints = [c for c in ProjectMemoryEvent.__table__.constraints if isinstance(c, UniqueConstraint)]
        self.assertEqual(len(constraints), 2)
        self.assertEqual(len({c.name for c in constraints}), 2)

    def test_provider_schema_requires_explicit_fields_without_mutating_local_schema(self):
        original = ProjectMemoryContent.model_json_schema()
        saved = copy.deepcopy(original)
        def check(value):
            if isinstance(value, dict):
                if value.get('type') == 'object' and 'properties' in value:
                    self.assertEqual(set(value['required']), set(value['properties']))
                self.assertNotIn('default', value)
                for child in value.values(): check(child)
            elif isinstance(value, list):
                for child in value: check(child)
        check(provider_schema(original))
        self.assertEqual(original, saved)

    def test_embedded_vectors_reject_boolean_index_float32_overflow_and_underflow(self):
        cfg = config(OPENROUTER_API_KEY='fake', OPENROUTER_EMBEDDING_MODEL='fake', OPENROUTER_EMBEDDING_DIMENSIONS=3)
        for row in ({'index': False, 'embedding': [1, 0, 0]}, {'index': 0, 'embedding': [1e100, 0, 0]},
                    {'index': 0, 'embedding': [1e-100, 0, 0]}):
            with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'data': [row]}))) as client:
                with self.assertRaises(EmbeddingError): embed_texts(['text'], cfg, client)

    def test_edited_update_rejects_invalid_dates_before_persistence(self):
        for date in (None, 'invalid', True, -1):
            value = update()
            value['edited_message'] = value.pop('message')
            value['edited_message']['edit_date'] = date
            self.assertIsNone(authorized_message(value, USER_ID))

    def test_commands_accept_newlines_and_tabs(self):
        self.assertEqual(parse_query('/pendientes\nSIMA').target, 'sima')
        self.assertEqual(reasoning_question('/ask\tEstado de SIMA?'), 'Estado de SIMA?')

    def test_project_catalog_requires_authentication_before_database_read(self):
        cfg = settings(TELEGRAM_BOT_TOKEN='fake', TELEGRAM_USER_ID='123')
        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        app.dependency_overrides[get_settings] = lambda: cfg
        app.dependency_overrides[get_session] = lambda: session
        client = TestClient(app)
        self.assertEqual(client.get('/projects').status_code, 401)
        session.scalars.assert_not_called()
        self.assertEqual(client.get('/projects', headers={'Authorization':'Bearer fake'}).status_code, 200)

    def test_telegram_outages_and_rate_limits_are_retryable_but_bad_credentials_are_fatal(self):
        for code in (429, 500, 502, 503):
            with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(code, json={'parameters':{'retry_after':45}}))) as client:
                with self.assertRaises(RetryablePollingError) as caught: TelegramAPI(client, 'fake').call('getUpdates')
                if code == 429: self.assertEqual(caught.exception.retry_after, 45)
        for code in (401, 403, 409):
            with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(code))) as client:
                with self.assertRaises(PollingError) as caught: TelegramAPI(client, 'fake').call('getUpdates')
                self.assertNotIsInstance(caught.exception, RetryablePollingError)

    def test_telegram_transport_failure_hides_sensitive_details_and_retries(self):
        def fail(request): raise httpx.ConnectError('fake-secret', request=request)
        with httpx.Client(transport=httpx.MockTransport(fail)) as client:
            with self.assertRaises(RetryablePollingError) as caught: TelegramAPI(client, 'fake').call('getUpdates')
        self.assertNotIn('fake-secret', str(caught.exception))

    def test_polling_retains_offset_during_transient_failure(self):
        cfg = settings(TELEGRAM_BOT_TOKEN='fake', TELEGRAM_USER_ID='123')
        with patch('app.telegram.get_settings',return_value=cfg), patch('app.telegram.TelegramAPI'), patch(
            'app.telegram.poll_once', side_effect=[RetryablePollingError('temporary'), 77, KeyboardInterrupt]) as poll, patch('app.telegram.time.sleep') as sleep:
            with self.assertRaises(KeyboardInterrupt): run(8000)
        self.assertEqual([c.args[5] for c in poll.call_args_list], [None, None, 77])
        sleep.assert_called_once()

    def test_process_exit_between_poll_and_terminate_is_safe(self):
        child=MagicMock(); child.poll.return_value=None; child.terminate.side_effect=ProcessLookupError
        stop_child(child)

    def test_duplicate_legacy_chunk_groups_keep_only_one_canonical_selection(self):
        from app.models import SourceChunk
        repo = FakeSession(raw='same original text')
        version = chunk_version(config())
        rows = [SourceChunk(id=uuid4(), source_id=repo.source.id, chunk_version=name, logical_version=version,
            chunk_index=0, content=repo.source.raw_content, char_start=0, char_end=len(repo.source.raw_content)) for name in ('old-a','old-b')]
        repo.chunks = rows
        with repo.begin(): selected = ensure_source_chunks(repo, repo.source, config())
        self.assertEqual(sum(row.logical_version == version for row in rows), 1)
        self.assertEqual(selected, 'old-a')
        self.assertEqual(len(repo.chunks), 2)

    def worker_fixture(self, revision=1, cursor=1):
        item = state(is_dirty=True, change_revision=revision, refresh_after=NOW)
        captured = SimpleNamespace(previous_id=None, revision=revision, cursor=cursor, version_number=1, cutoff=NOW)
        data={'previous_memory':None,'sources':[],'tasks':[],'decisions':[],'chunks':[]}
        engine, connection = memory_fixtures.ProjectMemoryTests().claimed_engine()
        session=MagicMock(); session.__enter__.return_value=session
        session.get.return_value=item; session.scalar.return_value=item
        return item,captured,data,engine,connection,session

    def test_empty_batch_retains_unconsumed_events(self):
        item,captured,data,engine,connection,session=self.worker_fixture(revision=3,cursor=1)
        with patch('app.services.project_memory.Session',return_value=session), patch('app.services.project_memory.snapshot_data',return_value=(captured,data)), patch('app.services.project_memory.generate_memory') as paid:
            self.assertFalse(refresh_project(engine,item.project_id,settings(),now=NOW))
        self.assertTrue(item.is_dirty)
        self.assertEqual(item.reconciled_revision,1)
        self.assertEqual(item.refresh_after,NOW)
        self.assertIn('FOR UPDATE',str(session.scalar.call_args.args[0]))
        paid.assert_not_called()

    def test_empty_refresh_does_not_clear_newer_event(self):
        item,captured,data,engine,connection,session=self.worker_fixture()
        def locked(statement): item.change_revision=2; return item
        session.scalar.side_effect=locked
        with patch('app.services.project_memory.Session',return_value=session), patch('app.services.project_memory.snapshot_data',return_value=(captured,data)):
            refresh_project(engine,item.project_id,settings(),now=NOW)
        self.assertTrue(item.is_dirty)
        self.assertEqual(item.reconciled_revision,0)

    def test_failure_does_not_delay_a_newer_immediate_request(self):
        item,captured,data,engine,connection,session=self.worker_fixture()
        data['sources']=[{'source_id':str(uuid4())}]
        def failed(*args): item.change_revision=2; raise RuntimeError('private')
        with patch('app.services.project_memory.Session',return_value=session), patch('app.services.project_memory.snapshot_data',return_value=(captured,data)), patch('app.services.project_memory.generate_memory',side_effect=failed):
            self.assertFalse(refresh_project(engine,item.project_id,settings(),now=NOW))
        self.assertIsNone(item.retry_after)

    def test_failure_backoff_starts_after_slow_provider_call(self):
        item,captured,data,engine,connection,session=self.worker_fixture()
        data['sources']=[{'source_id':str(uuid4())}]
        with patch('app.services.project_memory.Session',return_value=session), patch('app.services.project_memory.snapshot_data',return_value=(captured,data)), patch('app.services.project_memory.generate_memory',side_effect=RuntimeError), patch('app.services.project_memory.datetime') as clock:
            clock.now.side_effect=[NOW,NOW+timedelta(minutes=5)]
            refresh_project(engine,item.project_id,settings())
        self.assertEqual(item.retry_after,NOW+timedelta(minutes=6))

    def test_unlock_failure_does_not_report_committed_version_as_failed(self):
        item,captured,data,engine,connection,session=self.worker_fixture()
        data['sources']=[{'source_id':str(uuid4())}]
        connection.scalar.side_effect=[True,True,RuntimeError]
        with patch('app.services.project_memory.Session',return_value=session), patch('app.services.project_memory.snapshot_data',return_value=(captured,data)), patch('app.services.project_memory.generate_memory',return_value={}), patch('app.services.project_memory.publish_memory'):
            self.assertTrue(refresh_project(engine,item.project_id,settings(),now=NOW))
        connection.invalidate.assert_called_once()

    def test_revision_gap_is_stale_even_if_dirty_flag_was_cleared(self):
        fixture=memory_fixtures.ProjectMemoryTests()
        session,scope,sid,command=fixture.stale_fixture()
        rows=session.execute.side_effect
        values=list(rows); rows=iter(values)
        item=values[0].all()[0][0]; item.is_dirty=False
        session.execute.side_effect=rows
        memories,delta,_=retrieve_project_memories(session,scope,settings())
        self.assertTrue(memories[0]['is_dirty'])
        self.assertTrue(delta)

    def test_healthcheck_rejects_old_or_missing_migration_revision(self):
        from app.api.routes.health import database_health
        from fastapi import HTTPException
        for revisions in ([], ['0007_hierarchical_extraction']):
            with patch('app.api.routes.health.get_engine') as engine:
                conn=engine.return_value.connect.return_value.__enter__.return_value
                conn.execute.return_value.scalars.return_value.all.return_value=revisions
                with self.assertRaises(HTTPException) as caught: database_health()
                self.assertEqual(caught.exception.status_code,503)

    def test_live_worker_uses_a_fresh_clock_for_each_project(self):
        from app.project_memory_worker import run_once
        first,second=uuid4(),uuid4()
        with patch('app.project_memory_worker.Session'), patch('app.project_memory_worker.candidates',return_value=[(first,False),(second,False)]), patch('app.project_memory_worker.refresh_project',return_value=True) as refresh:
            self.assertEqual(run_once(MagicMock(),settings()),2)
        self.assertTrue(all(call.kwargs['now'] is None for call in refresh.call_args_list))

    def test_snapshot_failure_cannot_delay_newer_request(self):
        item,captured,data,engine,connection,session=self.worker_fixture()
        def failed(*args):
            item.change_revision=2
            raise RuntimeError('Context budget exceeded after a concurrent change')
        with patch('app.services.project_memory.Session',return_value=session), patch('app.services.project_memory.snapshot_data',side_effect=failed):
            self.assertFalse(refresh_project(engine,item.project_id,settings(),now=NOW))
        self.assertIsNone(item.retry_after)
