import copy
import hashlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4
import httpx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from app.config import get_settings
from app.database import get_session
from app.main import app
from app.models import DocumentAsset, SourceProcessingJob
from app.services.audio import AudioError, download_audio
from app.services.documents import (DocumentError, authorized_document, decode_document, document_scope,
    ingest_document, validate_document)
from app.services.project_matching import allows_content_project
from app.telegram import process_update, PollingError
from tests.test_telegram import update, project, USER_ID
from tests.test_reasoning import settings


def document(filename='meeting.txt', caption=None, update_id=100):
    payload = update(update_id=update_id)
    del payload['message']['text']
    payload['message']['document'] = {'file_id': 'fake-file', 'file_unique_id': 'fake-unique',
        'file_name': filename, 'mime_type': 'text/plain'}
    if caption is not None:
        payload['message']['caption'] = caption
    return payload


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.cfg = settings(TELEGRAM_BOT_TOKEN='fake-token', TELEGRAM_USER_ID=str(USER_ID))
        self.session = MagicMock()
        self.source = SimpleNamespace(id=uuid4(), primary_project_id=None, source_type='document_text')
        self.asset = SimpleNamespace(filename='meeting.txt')
        self.job = SimpleNamespace(status='pending')
        app.dependency_overrides[get_settings] = lambda: self.cfg
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def post(self, payload):
        return self.client.post('/telegram/updates', json=payload, headers={'Authorization': 'Bearer fake-token'})

    def test_txt_md_and_optional_unique_id_are_valid(self):
        for name in ['meeting.txt', 'reunión.md', 'README.MD']:
            message = document(name)['message']
            del message['document']['file_unique_id']
            self.assertEqual(validate_document(message, 20971520), name)
        # Extension wins over an unhelpful MIME type.
        message['document']['mime_type'] = 'application/octet-stream'
        validate_document(message, 20971520)

    def test_filename_paths_extensions_and_bad_metadata_rejected_before_download(self):
        for name in ['../x.txt', 'C:\\x.txt', '/etc/x.md', 'http://evil/x.txt', 'x.pdf', 'x.docx', '',
                     'x\n.txt', 'x.txt ', 'x' * 256 + '.txt', 'a/b.txt', '..\\x.md']:
            with self.subTest(name=name), patch('app.services.documents.download_audio') as download:
                response = self.post(document(name))
                self.assertEqual(response.json()['status'], 'answered')
                self.assertNotIn('source_id', response.json())
                download.assert_not_called()
        for key, value in [('file_size', None), ('file_size', True), ('file_size', -1), ('file_size', 0), ('file_size', 20971521),
                           ('file_id', ''), ('file_unique_id', 123), ('mime_type', 'x' * 101)]:
            payload = document(); payload['message']['document'][key] = value
            with self.subTest(key=key, value=value), patch('app.services.documents.download_audio') as download:
                self.assertEqual(self.post(payload).json()['status'], 'answered')
                download.assert_not_called()

    def test_authorization_precedes_document_validation_or_download(self):
        for field, key, value in [('from', 'id', 999), ('from', 'is_bot', True), ('chat', 'type', 'group'), ('chat', 'id', 999)]:
            payload = document(); payload['message'][field][key] = value
            self.assertIsNone(authorized_document(payload, USER_ID))
            with patch('app.api.routes.telegram.ingest_document') as ingest:
                self.assertEqual(self.post(payload).json()['status'], 'ignored')
                ingest.assert_not_called()
        with patch('app.api.routes.telegram.ingest_document') as ingest:
            self.assertEqual(self.client.post('/telegram/updates', json=document()).status_code, 401)
            ingest.assert_not_called()

    def test_strict_utf8_is_not_partially_stored(self):
        self.assertEqual(decode_document('á\n'.encode()), 'á\n')
        with self.assertRaisesRegex(DocumentError, 'El archivo no está codificado en UTF-8 y no pudo procesarse.'):
            decode_document(b'correct text\xff')
        self.session.scalar.return_value = None
        with patch('app.services.documents.download_audio', return_value=b'text\xff'):
            response = self.post(document())
        self.assertEqual(response.json()['answer'], 'El archivo no está codificado en UTF-8 y no pudo procesarse.')
        self.session.add.assert_not_called()
        for data in [b'', b'  \n', b'text\x00']:
            with self.assertRaises(DocumentError): decode_document(data)

    def test_original_bytes_full_text_metadata_and_one_job_commit_together(self):
        data = ('SIMA: reunión\n' + 'mucho contenido\n' * 3000).encode()
        payload = document(); original = copy.deepcopy(payload)
        self.session.scalar.side_effect = [None, self.source, self.asset, self.job]
        with patch('app.services.documents.download_audio', return_value=data), patch(
            'app.services.documents.document_scope', return_value=('document_text', None, None)):
            result = ingest_document(self.session, payload, USER_ID, self.cfg)
        params = self.session.scalar.call_args_list[1].args[0].compile(dialect=postgresql.dialect()).params
        self.assertEqual(params['raw_content'], data.decode())
        self.assertEqual(params['raw_metadata'], original)
        self.assertEqual(params['external_id'], '100')
        rows = [call.args[0] for call in self.session.add.call_args_list]
        asset = next(row for row in rows if isinstance(row, DocumentAsset))
        job = next(row for row in rows if isinstance(row, SourceProcessingJob))
        self.assertEqual(asset.original_bytes, data)
        self.assertEqual(asset.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(asset.file_size, len(data))
        self.assertEqual(asset.source_id, job.source_id)
        self.assertIn('Procesamiento: encolado', result['answer'])
        self.assertEqual(self.session.begin.call_count, 2)
        self.assertEqual(payload, original)

    def test_duplicate_update_reuses_asset_and_job_without_downloading(self):
        self.session.scalar.side_effect = [self.source, self.asset, self.job]
        with patch('app.services.documents.download_audio') as download:
            result = ingest_document(self.session, document(), USER_ID, self.cfg)
        self.assertEqual(result['status'], 'duplicate')
        download.assert_not_called()
        self.session.add.assert_not_called()

    def test_concurrent_insert_loser_reuses_committed_document(self):
        self.session.scalar.side_effect = [None, None, self.source, self.asset, self.job]
        with patch('app.services.documents.download_audio', return_value=b'content'), patch(
            'app.services.documents.document_scope', return_value=('document_text', None, None)):
            result = ingest_document(self.session, document(), USER_ID, self.cfg)
        self.assertEqual(result['status'], 'duplicate')
        self.session.add.assert_not_called()

    def test_caption_forms_unknown_ambiguous_and_body_matching_are_conservative(self):
        sima, alma = project('SIMA', 'CIMA'), project('ALMA')
        self.session.scalars.return_value.all.return_value = [sima, alma]
        for caption, kind in [('SIMA', 'document_text'), ('/proyecto CIMA', 'document_text'),
                              ('/reunion SIMA', 'meeting_transcript'), ('/documento SIMA', 'document_text')]:
            self.assertEqual(document_scope(self.session, 'ALMA in body', caption), (kind, sima, None))
        kind, selected, warning = document_scope(self.session, 'SIMA in body', '/proyecto NoExiste')
        self.assertIsNone(selected)
        self.assertIn('no encontré ese proyecto', warning)
        self.session.scalars.return_value.all.return_value = [sima, project('Other', 'SIMA')]
        _, selected, warning = document_scope(self.session, 'text', 'SIMA')
        self.assertIsNone(selected)
        self.assertIn('ambiguo', warning)
        self.session.scalars.return_value.all.return_value = [sima, alma]
        self.assertIs(document_scope(self.session, 'CIMA reunión', '')[1], sima)
        self.assertIsNone(document_scope(self.session, 'SIMA y ALMA reunión', '')[1])
        self.assertIsNone(document_scope(self.session, 'Nada identificado', '')[1])

    def test_explicit_unknown_caption_blocks_later_content_reassignment(self):
        self.source.raw_metadata = document(caption='/proyecto NoExiste')
        self.assertFalse(allows_content_project(self.source))
        self.source.raw_metadata = document()
        self.assertTrue(allows_content_project(self.source))

    def test_download_checks_actual_size_paths_redirects_and_sanitizes_failures(self):
        for path in ['../secret', '/absolute', 'https://evil/file', 'documents/%2e%2e/x', 'documents/./x', 'documents//x']:
            def transport(request): return httpx.Response(200, json={'ok': True, 'result': {'file_path': path}})
            with httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False) as client, self.assertRaises(AudioError):
                download_audio('fake', {'file_id': 'fake'}, 1024, client)
        for response in [httpx.Response(200, content=b'x' * 1025), httpx.Response(302, headers={'Location': 'https://evil'})]:
            def transport(request):
                return httpx.Response(200, json={'ok': True, 'result': {'file_path': 'documents/file.txt'}}) if request.method == 'POST' else response
            with httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False) as client, self.assertRaises(AudioError):
                download_audio('fake', {'file_id': 'fake'}, 1024, client)
        with patch('app.services.documents.download_audio', side_effect=AudioError('secret URL fake-token')):
            self.session.scalar.return_value = None
            response = self.post(document())
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('fake-token', response.text)

    def test_document_route_and_poller_never_process_or_index_inline(self):
        queued = {'status': 'saved', 'source_id': str(uuid4()), 'media_type': 'document', 'answer': 'Procesamiento: encolado'}
        with patch('app.api.routes.telegram.ingest_document', return_value=queued), patch('app.api.routes.telegram.maybe_index') as index, patch(
            'app.api.routes.telegram.route_intent') as classify:
            self.assertEqual(self.post(document()).json(), queued)
            index.assert_not_called(); classify.assert_not_called()
        telegram = MagicMock()
        with patch('app.telegram.forward_update', return_value=queued), patch('app.telegram.process_saved_source') as process:
            process_update(MagicMock(), telegram, 'http://localhost', 'fake', USER_ID, document(), processing=True)
            process.assert_not_called()
            self.assertIn('encolado', telegram.call.call_args.args[1]['text'])
        telegram.call.side_effect = PollingError('unavailable')
        with patch('app.telegram.forward_update', return_value=queued), patch('app.telegram.process_saved_source') as process:
            process_update(MagicMock(), telegram, 'http://localhost', 'fake', USER_ID, document(), processing=True)
            process.assert_not_called()

    def test_additive_migration_immutability_and_disabled_downgrade(self):
        buffer = io.StringIO()
        with patch('app.config.get_settings', return_value=settings(DATABASE_URL='postgresql://offline/test')):
            command.upgrade(Config('alembic.ini', output_buffer=buffer), '0009_project_memory:0010_documents_jobs', sql=True)
        sql = buffer.getvalue()
        self.assertIn('CREATE TABLE document_assets', sql)
        self.assertIn('CREATE TABLE source_processing_jobs', sql)
        self.assertIn('BEFORE UPDATE OR DELETE ON document_assets', sql)
        self.assertIn('UNIQUE (source_id)', sql)
        for word in ['DROP TABLE', 'TRUNCATE', 'DELETE FROM', 'ALTER TABLE sources']:
            self.assertNotIn(word, sql)
