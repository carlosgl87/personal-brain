import io
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4
from sqlalchemy.dialects import postgresql
from app.services.source_processing_jobs import (claim_job, finish_job, candidate_ids, answer_processing,
    processing_argument, lock_id, MAX_ATTEMPTS)
from app.source_processing_worker import run_once, preflight, retry_source, notify_completed
from tests.test_reasoning import settings

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def job(attempts=0):
    return SimpleNamespace(source_id=uuid4(), status='pending', attempts=attempts, claim_token=None,
        claimed_at=None, next_retry_at=NOW, completed_at=None, last_error_code=None)


class SourceWorkerTests(unittest.TestCase):
    def test_claim_skip_locked_attempts_and_crash_recovery(self):
        session = MagicMock(); item = job(); session.scalar.return_value = item
        token = claim_job(session, item.source_id, NOW)
        self.assertEqual(item.status, 'processing')
        self.assertEqual(item.attempts, 1)
        self.assertEqual(item.claim_token, token)
        sql = str(session.scalar.call_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn('FOR UPDATE SKIP LOCKED', sql)
        self.assertIn('processing', repr(session.scalar.call_args.args[0].compile().params))
        # Recovery of a processing row is permitted only after acquiring session ownership.
        next_token = claim_job(session, item.source_id, NOW + timedelta(seconds=1))
        self.assertNotEqual(next_token, token)
        self.assertEqual(item.attempts, 2)

    def test_max_attempts_stop_even_when_previous_worker_crashed(self):
        session = MagicMock(); item = job(MAX_ATTEMPTS); item.status = 'processing'
        session.scalar.return_value = item
        self.assertIsNone(claim_job(session, item.source_id, NOW))
        self.assertEqual(item.status, 'failed')
        self.assertEqual(item.last_error_code, 'attempts_exhausted')

    def test_backoff_and_attempt_limit(self):
        for attempts, seconds in [(1, 60), (2, 300), (3, 900), (4, 3600), (5, 3600), (6, 3600)]:
            session = MagicMock(); item = job(attempts); session.scalar.return_value = item
            self.assertTrue(finish_job(session, item.source_id, uuid4(), NOW, 'processing_failed'))
            self.assertEqual(item.next_retry_at, NOW + timedelta(seconds=seconds))
            self.assertEqual(item.status, 'failed' if attempts == 6 else 'retry')

    def test_old_claim_cannot_overwrite_new_attempt_and_completion_clears_error(self):
        session = MagicMock(); session.scalar.return_value = None
        token = uuid4()
        self.assertFalse(finish_job(session, uuid4(), token, NOW))
        sql = session.scalar.call_args.args[0].compile(dialect=postgresql.dialect())
        self.assertEqual(sql.params['claim_token_1'], token)
        item = job(2); item.last_error_code = 'processing_failed'; session.scalar.return_value = item
        self.assertTrue(finish_job(session, item.source_id, token, NOW))
        self.assertEqual(item.status, 'completed')
        self.assertEqual(item.completed_at, NOW)
        self.assertIsNone(item.last_error_code)

    def fixture(self):
        engine, connection, session = MagicMock(), MagicMock(), MagicMock()
        engine.connect.return_value = connection
        connection.execution_options.return_value = connection
        connection.scalar.return_value = True
        session.__enter__.return_value = session
        return engine, connection, session

    def test_worker_reuses_index_then_process_and_notifies_only_after_committed_success(self):
        engine, connection, session = self.fixture(); sid, token = uuid4(), uuid4(); order=[]
        with patch('app.source_processing_worker.Session', return_value=session), patch(
            'app.source_processing_worker.candidate_ids', return_value=[sid]), patch('app.source_processing_worker.claim_job', return_value=token), patch(
            'app.source_processing_worker.preflight'), patch('app.source_processing_worker.index_source', side_effect=lambda *a: order.append('index')), patch(
            'app.source_processing_worker.process_source', side_effect=lambda *a: order.append('process') or {'status': 'already_processed'}), patch(
            'app.source_processing_worker.finish_job', side_effect=lambda *a: order.append('complete') or True), patch(
            'app.source_processing_worker.notify_completed', side_effect=lambda *a: order.append('notify')):
            self.assertEqual(run_once(engine, settings(), NOW), 1)
        self.assertEqual(order, ['index', 'process', 'complete', 'notify'])
        self.assertIn('pg_advisory_unlock', str(connection.scalar.call_args.args[0]))
        connection.close.assert_called_once()

    def test_worker_failure_schedules_retry_without_notification_or_sensitive_details(self):
        engine, connection, session = self.fixture(); sid, token = uuid4(), uuid4()
        with patch('app.source_processing_worker.Session', return_value=session), patch(
            'app.source_processing_worker.candidate_ids', return_value=[sid]), patch('app.source_processing_worker.claim_job', return_value=token), patch(
            'app.source_processing_worker.preflight'), patch('app.source_processing_worker.index_source', side_effect=RuntimeError('secret-provider-body')), patch(
            'app.source_processing_worker.process_source') as process, patch('app.source_processing_worker.finish_job', return_value=True) as finish, patch(
            'app.source_processing_worker.notify_completed') as notify:
            self.assertEqual(run_once(engine, settings(), NOW), 1)
        self.assertEqual(finish.call_args.args[-1], 'processing_failed')
        process.assert_not_called(); notify.assert_not_called()

    def test_busy_lock_never_claims_or_calls_models(self):
        engine, connection, session = self.fixture(); connection.scalar.return_value = False
        with patch('app.source_processing_worker.Session', return_value=session), patch(
            'app.source_processing_worker.candidate_ids', return_value=[uuid4()]), patch('app.source_processing_worker.claim_job') as claim, patch(
            'app.source_processing_worker.index_source') as index:
            self.assertEqual(run_once(engine, settings(), NOW), 0)
        claim.assert_not_called(); index.assert_not_called()
        self.assertEqual(connection.scalar.call_count, 1)

    def test_lost_connection_does_not_publish_old_claim(self):
        engine, connection, session = self.fixture()
        connection.scalar.side_effect = [True, RuntimeError('secret'), RuntimeError('secret')]
        with patch('app.source_processing_worker.Session', return_value=session), patch(
            'app.source_processing_worker.candidate_ids', return_value=[uuid4()]), patch('app.source_processing_worker.claim_job', return_value=uuid4()), patch(
            'app.source_processing_worker.preflight'), patch('app.source_processing_worker.index_source'), patch(
            'app.source_processing_worker.process_source', return_value={'status': 'processed'}), patch('app.source_processing_worker.finish_job') as finish, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run_once(engine, settings(), NOW), 0)
        finish.assert_not_called()
        connection.invalidate.assert_called_once()
        self.assertNotIn('secret', output.getvalue())

    def test_disabled_worker_has_no_database_access(self):
        engine = MagicMock()
        self.assertEqual(run_once(engine, settings(SOURCE_PROCESSING_WORKER_ENABLED=False)), 0)
        engine.connect.assert_not_called()

    def test_oversized_extraction_fails_before_paid_embeddings(self):
        session = MagicMock(); source = SimpleNamespace(raw_content='x'*31000); session.get.return_value = source
        with patch('app.source_processing_worker.chunk_source', return_value=list(range(41))):
            with self.assertRaisesRegex(ValueError, 'extraction_limit'):
                preflight(session, uuid4(), settings())

    def test_notification_failure_is_safe_and_does_not_touch_queue(self):
        engine, connection, session = self.fixture()
        session.get.side_effect = [SimpleNamespace(external_source='telegram', primary_project_id=None)]
        session.scalar.return_value = SimpleNamespace(filename='meeting.txt')
        cfg=settings(TELEGRAM_BOT_TOKEN='fake-token', TELEGRAM_USER_ID='123')
        with patch('app.source_processing_worker.Session', return_value=session), patch('app.source_processing_worker.httpx.Client'), patch(
            'app.source_processing_worker.TelegramAPI') as api, redirect_stdout(io.StringIO()) as output:
            api.return_value.call.side_effect = RuntimeError('private-token')
            notify_completed(engine, uuid4(), {'tasks_count': 4, 'decisions_count': 2}, cfg)
        self.assertNotIn('private-token', output.getvalue())
        self.assertIn('notification unavailable', output.getvalue())
        session.add.assert_not_called()
        self.assertIn('Tareas: 4', api.return_value.call.call_args.args[1]['text'])

    def test_status_is_compact_read_only_and_argument_is_exact(self):
        self.assertEqual(processing_argument('/procesamiento'), '')
        self.assertIsNone(processing_argument('/proyecto SIMA'))
        session=MagicMock(); session.execute.return_value.all.return_value=[]
        answer=answer_processing(session, '')
        self.assertIn('Completados recientes', answer)
        self.assertEqual(session.execute.call_count, 5)
        session.add.assert_not_called()
        self.assertIn('Usa /procesamiento', answer_processing(session, 'bad uuid'))

    def test_manual_retry_refuses_running_ownership(self):
        engine, connection, session = self.fixture(); connection.__enter__.return_value=connection
        connection.scalar.return_value=False
        self.assertFalse(retry_source(engine, uuid4()))
        self.assertEqual(connection.scalar.call_count, 1)

    def test_cloud_restarts_source_worker_without_stopping_api_bot_or_memory(self):
        from app.cloud import supervise
        cfg=MagicMock(); cfg.source_processing_worker_enabled=True; cfg.project_memory_enabled=True
        cfg.telegram_credentials.return_value=('fake', 123)
        api,bot,memory,dead,live=[MagicMock() for _ in range(5)]
        for child in [api,bot,memory,live]: child.poll.return_value=None
        dead.poll.return_value=1
        engine,connection,_=self.fixture(); stop=threading.Event(); ticks=[]
        def wait(_):
            for child in [api,bot,memory]: child.terminate.assert_not_called()
            ticks.append(1)
            if len(ticks)==4: stop.set()
        with patch('app.cloud.get_settings', return_value=cfg), patch('app.cloud.get_engine', return_value=engine), patch(
            'app.cloud.wait_api', return_value=True), patch('app.cloud.subprocess.Popen', side_effect=[api,bot,memory,dead,live]) as spawn, patch(
            'app.cloud.time.monotonic', side_effect=[0,0,0,0,31]), patch.object(stop,'wait',side_effect=wait):
            self.assertEqual(supervise(8000, stop), 0)
        self.assertEqual(spawn.call_args_list[3].args[0][-1], 'app.source_processing_worker')
        self.assertEqual(spawn.call_args_list[4].args[0][-1], 'app.source_processing_worker')
        live.terminate.assert_called_once()
