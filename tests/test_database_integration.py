"""Opt-in PostgreSQL integration tests. Never load .env or call paid providers.

Use only a disposable database: PERSONAL_BRAIN_TEST_DATABASE_URL must be a loopback
URL whose database name starts with personal_brain_test_, or the isolated PGlite
fixture at 127.0.0.1:55439/postgres. Apply migrations before running these tests.
These tests append fixtures and deliberately do not delete immutable history.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from unittest.mock import patch
from sqlalchemy import create_engine, select, text, func
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool, QueuePool
from app.models import (Area, Project, Source, Task, TaskChange, ProcessingRun, SourceChunk,
    ChunkEmbedding, ProjectMemoryState, ProjectMemoryVersion, ProjectMemoryEvent, TaskEvidence, GenerationPart)
from app.seed import seed
from app.schemas.extraction import Extraction
from app.services.processing import process_text_source, SourceNotProcessable
from app.services.project_memory import snapshot_data, publish_memory
from app.services.project_memory_events import mark_dirty
from app.services.project_memory_retrieval import retrieve_project_memories
from app.services.queries import Scope, task_statement, source_statement
from app.services.memory import index_source, semantic_search
from app.services.task_management import TaskCommand, edit_task
from test_hierarchical import config, output, QUOTE, FakeClaude
from test_project_memory import memory

TEST_URL=os.environ.get('PERSONAL_BRAIN_TEST_DATABASE_URL')


@unittest.skipUnless(TEST_URL, 'Requires an explicitly configured disposable local database')
class DatabaseIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url=make_url(TEST_URL)
        safe_name=(url.database or '').startswith('personal_brain_test_')
        pglite=url.host == '127.0.0.1' and url.port == 55439 and url.database == 'postgres'
        if url.host not in {'127.0.0.1','localhost','::1'} or not (safe_name or pglite):
            raise ValueError('Integration tests require a disposable loopback database.')
        cls.engine=create_engine(url,poolclass=QueuePool if pglite else NullPool,hide_parameters=True,
            connect_args={"prepare_threshold":None} if pglite else {},
            **({"pool_size":1,"max_overflow":0} if pglite else {}))
        with cls.engine.connect() as conn:
            if conn.scalar(text('SELECT version_num FROM alembic_version')) != '0010_documents_jobs':
                raise ValueError('Apply the current migrations to the disposable database first.')
        with Session(cls.engine) as session,session.begin(): seed(session)

    @classmethod
    def tearDownClass(cls): cls.engine.dispose()

    def setUp(self):
        self.cfg=config(PROJECT_MEMORY_ENABLED=True,PROJECT_MEMORY_DEBOUNCE_SECONDS=0)
        self.now=datetime.now(timezone.utc)
        with Session(self.engine) as session,session.begin():
            area=session.scalar(select(Area).limit(1))
            self.a=uuid4(); self.b=uuid4()
            session.add_all([Project(id=self.a,area_id=area.id,name='Audit '+str(self.a),slug=str(self.a)),
                             Project(id=self.b,area_id=area.id,name='Audit '+str(self.b),slug=str(self.b))])

    def save_source(self,project_id=None,content=QUOTE,source_type='meeting_transcript',metadata=None):
        source_id=uuid4()
        with Session(self.engine) as session,session.begin():
            session.add(Source(id=source_id,source_type=source_type,raw_content=content,raw_metadata=metadata or {},primary_project_id=project_id))
        return source_id

    def process(self,source_id,force=False):
        with Session(self.engine) as session,patch('app.services.processing.extract',return_value=Extraction.model_validate(output(task=True))):
            return process_text_source(session,source_id,self.cfg,force=force)

    def envelope(self,edited=False,seconds=0):
        message={'message_id':10,'date':1700000000,'from':{'id':123,'is_bot':False},'chat':{'id':123,'type':'private'},'text':QUOTE}
        if edited: message['edit_date']=1700000000+seconds
        # Every test uses a distinct chat so old fixtures cannot supersede each other.
        message['chat']['id']=self.a.int % 1000000000
        return {'update_id':seconds,'edited_message' if edited else 'message':message}

    def test_seed_is_additive_and_idempotent_with_existing_projects(self):
        with Session(self.engine) as session,session.begin():
            before=session.scalar(select(func.count(Project.id)))
            seed(session); seed(session)
            self.assertEqual(session.scalar(select(func.count(Project.id))),before)
            self.assertGreaterEqual(before,32)

    def test_processing_is_idempotent_and_current_queries_use_latest_run(self):
        sid=self.save_source(self.a)
        first=self.process(sid); second=self.process(sid)
        self.assertEqual(first['run_id'],second['run_id'])
        replacement=self.process(sid,force=True)
        with Session(self.engine) as session:
            self.assertEqual(session.scalar(select(func.count(ProcessingRun.id)).where(ProcessingRun.source_id==sid)),2)
            tasks=session.execute(task_statement(Scope([self.a],'A'))).all()
            self.assertEqual(len(tasks),1)
            self.assertEqual(str(tasks[0][0].processing_run_id),replacement['run_id'])
            self.assertEqual(session.scalar(select(func.count(ProjectMemoryEvent.id)).where(ProjectMemoryEvent.project_id==self.a)),2)

    def test_original_sources_and_memory_history_are_immutable_in_database(self):
        sid=self.save_source(self.a)
        self.process(sid)
        with Session(self.engine) as session:
            with session.begin(): captured,data=snapshot_data(session,self.a,self.cfg)
            with session.begin(): version=publish_memory(session,self.a,captured,data,memory(sid).model_dump(mode='json'),self.cfg,False,self.now)
        for sql,identifier in [('UPDATE sources SET raw_content=\'overwritten\' WHERE id=:id',sid),
                               ('DELETE FROM project_memory_versions WHERE id=:id',version)]:
            # Catch the trigger exception inside PostgreSQL itself; this also works
            # with the embedded socket fixture's limited error-protocol handling.
            command=sql.replace(':id', "'"+str(identifier)+"'::uuid")
            block="DO $$ DECLARE rejected boolean := false; BEGIN BEGIN "+command+"; EXCEPTION WHEN SQLSTATE 'P0001' THEN rejected := true; END; IF NOT rejected THEN RAISE EXCEPTION 'Immutability trigger did not reject the change'; END IF; END $$;"
            with self.engine.begin() as conn:
                conn.exec_driver_sql(block)
        with Session(self.engine) as session:
            self.assertEqual(session.get(Source,sid).raw_content,QUOTE)
            self.assertIsNotNone(session.get(ProjectMemoryVersion,version))

    def test_new_event_during_snapshot_keeps_memory_dirty_and_retrieves_delta(self):
        sid=self.save_source(self.a); self.process(sid)
        with Session(self.engine) as session,session.begin(): captured,data=snapshot_data(session,self.a,self.cfg)
        second=self.save_source(self.a,content='A new confirmed change.'); self.process(second)
        with Session(self.engine) as session,session.begin():
            publish_memory(session,self.a,captured,data,memory(sid).model_dump(mode='json'),self.cfg,False,self.now)
        with Session(self.engine) as session:
            state=session.get(ProjectMemoryState,self.a)
            self.assertTrue(state.is_dirty)
            memories,delta,warnings=retrieve_project_memories(session,Scope([self.a],'A'),self.cfg)
            self.assertTrue(memories[0]['is_dirty'])
            self.assertIn(str(second),{item['source_id'] for item in delta})

    def test_edit_that_moves_note_invalidates_old_and_new_project(self):
        old=self.save_source(self.a,source_type='telegram_text',metadata=self.envelope()); self.process(old)
        with Session(self.engine) as session,session.begin():
            captured,data=snapshot_data(session,self.a,self.cfg)
            publish_memory(session,self.a,captured,data,memory(old).model_dump(mode='json'),self.cfg,False,self.now)
        new=self.save_source(self.b,source_type='telegram_text',metadata=self.envelope(edited=True,seconds=10)); self.process(new)
        with Session(self.engine) as session:
            self.assertTrue(session.get(ProjectMemoryState,self.a).is_dirty)
            self.assertTrue(session.get(ProjectMemoryState,self.b).is_dirty)
            self.assertEqual(session.execute(task_statement(Scope([self.a],'A'))).all(),[])
            self.assertEqual(len(session.execute(task_statement(Scope([self.b],'B'))).all()),1)
            self.assertIsNotNone(session.get(Source,old))
            memories,delta,warnings=retrieve_project_memories(session,Scope([self.a],'A'),self.cfg)
            retractions=[item for item in delta if item.get('event_type')=='source_revision']
            self.assertEqual(retractions[0]['superseded_source_ids'],[str(old)])
            self.assertEqual(retractions[0]['current_source_project_id'],str(self.b))
            self.assertNotIn('excerpt',retractions[0])
            captured,data=snapshot_data(session,self.a,self.cfg)
            self.assertEqual(data['changes'][0]['superseded_source_ids'],[str(old)])

    def test_new_telegram_edit_cannot_discard_manually_completed_task(self):
        sid=self.save_source(self.a,source_type='telegram_text',metadata=self.envelope()); self.process(sid)
        with Session(self.engine) as session:
            task_id=session.scalar(select(Task.id).where(Task.source_id==sid))
        command=self.save_source(content='/completar '+str(task_id),source_type='telegram_query')
        with Session(self.engine) as session: edit_task(session,TaskCommand('completar',task_id,''),command,settings=self.cfg)
        edited=self.save_source(self.b,source_type='telegram_text',metadata=self.envelope(edited=True,seconds=20))
        with Session(self.engine) as session,patch('app.services.processing.extract') as paid:
            with self.assertRaises(SourceNotProcessable): process_text_source(session,edited,self.cfg)
            paid.assert_not_called()
        with Session(self.engine) as session:
            self.assertEqual(session.get(Task,task_id).status,'completed')
            self.assertIsNone(session.get(Source,edited).latest_processing_run_id)
            self.assertIn(sid,{source.id for source,run in session.execute(source_statement(Scope([self.a],'A'))).all()})

    def test_chunk_vector_versions_reuse_text_without_repeating_embeddings(self):
        sid=self.save_source(self.a)
        cfg=config(OPENROUTER_API_KEY='fake',OPENROUTER_EMBEDDING_MODEL='audit-v1',OPENROUTER_EMBEDDING_DIMENSIONS=3)
        with patch('app.services.memory.embed_texts',return_value=[[1,0,0]]) as provider:
            with Session(self.engine) as session: first=index_source(session,sid,cfg)
            with Session(self.engine) as session: second=index_source(session,sid,cfg)
            self.assertEqual(provider.call_count,1)
            cfg2=config(OPENROUTER_API_KEY='fake',OPENROUTER_EMBEDDING_MODEL='audit-v2',OPENROUTER_EMBEDDING_DIMENSIONS=3)
            with Session(self.engine) as session: index_source(session,sid,cfg2)
        with Session(self.engine) as session:
            self.assertEqual(session.scalar(select(func.count(SourceChunk.id)).where(SourceChunk.source_id==sid)),1)
            self.assertEqual(session.scalar(select(func.count(ChunkEmbedding.id)).join(SourceChunk).where(SourceChunk.source_id==sid)),2)
            with patch('app.services.memory.embed_texts',return_value=[[1,0,0]]):
                matches=semantic_search(session,'report',cfg2,project_ids=[self.a])
            self.assertEqual(matches[0]['source_id'],str(sid))
            self.assertAlmostEqual(matches[0]['distance'],0)

    def test_hierarchical_processing_reuses_partials_and_preserves_generation_evidence(self):
        sid=self.save_source(self.a,content=QUOTE+' '+'z '*18000)
        provider=FakeClaude()
        with patch('app.services.hierarchical_llm.call_json',side_effect=provider):
            with Session(self.engine) as session: process_text_source(session,sid,self.cfg)
            count=provider.part_calls
            with Session(self.engine) as session: process_text_source(session,sid,self.cfg,force=True)
            self.assertEqual(provider.part_calls,count)
            with Session(self.engine) as session: process_text_source(session,sid,self.cfg,force=True,refresh_parts=True)
            self.assertGreater(provider.part_calls,count)
        with Session(self.engine) as session:
            self.assertGreater(session.scalar(select(func.count(GenerationPart.id))),0)
            self.assertGreater(session.scalar(select(func.count(TaskEvidence.id)).join(Task).where(Task.source_id==sid)),0)
            self.assertEqual(session.scalar(select(func.count(ProcessingRun.id)).where(ProcessingRun.source_id==sid)),3)

    def test_event_duplicate_and_source_change_commit_atomically(self):
        sid=self.save_source(self.a)
        with Session(self.engine) as session,session.begin():
            self.assertTrue(mark_dirty(session,self.a,self.cfg,origin_key='test:'+str(sid),source_id=sid))
            self.assertFalse(mark_dirty(session,self.a,self.cfg,origin_key='test:'+str(sid),source_id=sid))
        with Session(self.engine) as session:
            self.assertEqual(session.get(ProjectMemoryState,self.a).change_revision,1)
        with Session(self.engine) as session:
            with self.assertRaises(RuntimeError):
                with session.begin():
                    mark_dirty(session,self.a,self.cfg,origin_key='rollback:'+str(sid),source_id=sid)
                    raise RuntimeError('rollback')
        with Session(self.engine) as session:
            self.assertEqual(session.get(ProjectMemoryState,self.a).change_revision,1)
    def save_document(self, caption='SIMA', content='Ana debe enviar el informe mañana.', update_id=None):
        from tests.test_documents import document
        from app.services.documents import ingest_document
        cfg=config(TELEGRAM_BOT_TOKEN='fake-token', TELEGRAM_USER_ID='12345', PROJECT_MEMORY_ENABLED=True)
        payload=document(caption=caption, update_id=update_id or uuid4().int % 1000000000000)
        with Session(self.engine) as session, patch('app.services.documents.download_audio', return_value=content.encode('utf-8')):
            result=ingest_document(session,payload,12345,cfg)
        return result,payload,cfg

    def test_document_asset_source_job_are_atomic_idempotent_and_new_updates_are_new_sources(self):
        from app.models import DocumentAsset,SourceProcessingJob
        from app.services.documents import ingest_document
        result,payload,cfg=self.save_document()
        sid=UUID(result['source_id'])
        with Session(self.engine) as session, patch('app.services.documents.download_audio') as download:
            repeat=ingest_document(session,payload,12345,cfg)
        self.assertEqual(repeat['source_id'],result['source_id']); download.assert_not_called()
        payload['update_id']+=1
        with Session(self.engine) as session, patch('app.services.documents.download_audio', return_value=b'Ana debe enviar el informe manana.'):
            new=ingest_document(session,payload,12345,cfg)
        self.assertNotEqual(result['source_id'],new['source_id'])
        with Session(self.engine) as session:
            asset=session.scalar(select(DocumentAsset).where(DocumentAsset.source_id==sid))
            self.assertEqual(asset.original_bytes,'Ana debe enviar el informe mañana.'.encode())
            self.assertEqual(session.get(Source,sid).raw_content,asset.original_bytes.decode())
            self.assertEqual(session.scalar(select(func.count(SourceProcessingJob.id)).where(SourceProcessingJob.source_id==sid)),1)
            self.assertEqual(session.scalar(select(func.count(Task.id)).where(Task.source_id==sid)),0)
            self.assertEqual(session.scalar(select(func.count(ProjectMemoryEvent.id)).where(ProjectMemoryEvent.source_id==sid)),0)

    def test_document_job_insert_failure_rolls_back_source_and_asset(self):
        from app.services.documents import ingest_document
        from app.models import DocumentAsset,SourceProcessingJob
        from tests.test_documents import document
        payload=document(caption='SIMA',update_id=uuid4().int % 1000000000000)
        cfg=config(TELEGRAM_BOT_TOKEN='fake-token',TELEGRAM_USER_ID='12345')
        with Session(self.engine) as session, patch('app.services.documents.download_audio',return_value=b'text'):
            original_add=session.add
            def add(row):
                if isinstance(row,SourceProcessingJob): raise RuntimeError('simulated queue failure')
                original_add(row)
            with patch.object(session,'add',side_effect=add), self.assertRaises(RuntimeError):
                ingest_document(session,payload,12345,cfg)
        with Session(self.engine) as session:
            self.assertIsNone(session.scalar(select(Source).where(Source.external_source=='telegram',Source.external_id==str(payload['update_id']))))

    def test_document_original_bytes_cannot_be_updated_or_deleted(self):
        result,_,_=self.save_document()
        # Catch trigger violations inside SQL for compatibility with the embedded test protocol.
        sid=UUID(result['source_id'])
        for statement in ["UPDATE document_assets SET original_bytes=decode('00','hex') WHERE source_id='%s'" % sid,
                          "DELETE FROM document_assets WHERE source_id='%s'" % sid]:
            sql="DO $$ BEGIN BEGIN "+statement+"; RAISE EXCEPTION 'immutability missing' USING ERRCODE='XX000'; EXCEPTION WHEN SQLSTATE 'P0001' THEN NULL; END; END $$;"
            with self.engine.begin() as conn: conn.execute(text(sql))

    def test_queue_claim_completion_retry_and_stale_tokens_use_real_sql(self):
        from app.models import SourceProcessingJob
        from app.services.source_processing_jobs import claim_job,finish_job
        result,_,_=self.save_document(); sid=UUID(result['source_id'])
        with Session(self.engine) as session,session.begin():
            token=claim_job(session,sid,self.now+timedelta(seconds=1))
        self.assertIsNotNone(token)
        with Session(self.engine) as session,session.begin():
            self.assertFalse(finish_job(session,sid,uuid4(),self.now))
            self.assertTrue(finish_job(session,sid,token,self.now,'processing_failed'))
        with Session(self.engine) as session,session.begin():
            self.assertIsNone(claim_job(session,sid,self.now+timedelta(seconds=10)))
            second=claim_job(session,sid,self.now+timedelta(seconds=61))
            self.assertNotEqual(second,token)
        with Session(self.engine) as session,session.begin():
            self.assertFalse(finish_job(session,sid,token,self.now))
            self.assertTrue(finish_job(session,sid,second,self.now+timedelta(seconds=62)))
        with Session(self.engine) as session:
            self.assertEqual(session.scalar(select(SourceProcessingJob.status).where(SourceProcessingJob.source_id==sid)),'completed')

    def test_document_worker_reuses_extraction_and_notification_failure_keeps_completed(self):
        from app.source_processing_worker import run_once
        from app.models import SourceProcessingJob,DocumentAsset
        result,_,cfg=self.save_document(); sid=UUID(result['source_id'])
        with patch('app.source_processing_worker.candidate_ids',return_value=[sid]), patch(
            'app.services.processing.extract',return_value=Extraction.model_validate(output(task=True))) as extract, patch(
            'app.source_processing_worker.TelegramAPI') as api:
            api.return_value.call.side_effect=RuntimeError('fake secret')
            self.assertEqual(run_once(self.engine,cfg),1)
            self.assertEqual(run_once(self.engine,cfg),0)
            extract.assert_called_once()
        with Session(self.engine) as session:
            self.assertEqual(session.scalar(select(SourceProcessingJob.status).where(SourceProcessingJob.source_id==sid)),'completed')
            self.assertEqual(session.scalar(select(func.count(Task.id)).where(Task.source_id==sid)),1)
            self.assertEqual(session.scalar(select(func.count(ProcessingRun.id)).where(ProcessingRun.source_id==sid)),1)
            self.assertEqual(session.scalar(select(func.count(ProjectMemoryEvent.id)).where(ProjectMemoryEvent.source_id==sid)),1)
            self.assertIsNotNone(session.scalar(select(DocumentAsset).where(DocumentAsset.source_id==sid)))

    def test_unknown_document_caption_stays_unassigned_after_extraction(self):
        result,_,cfg=self.save_document(caption='/proyecto NoExiste',content='SIMA: Ana debe enviar el informe mañana.')
        sid=UUID(result['source_id'])
        with Session(self.engine) as session:
            sima=session.scalar(select(Project.id).where(Project.slug=='sima'))
        with Session(self.engine) as session,patch('app.services.processing.extract',return_value=Extraction.model_validate(output(project_id=sima,task=True))):
            process_text_source(session,sid,cfg)
        with Session(self.engine) as session:
            self.assertIsNone(session.get(Source,sid).primary_project_id)
            self.assertIsNone(session.scalar(select(Task.project_id).where(Task.source_id==sid)))

    def test_query_ingestion_and_catalog_do_not_create_tasks_or_memory_events(self):
        from app.services.telegram_ingestion import ingest_update
        from app.services.queries import catalog_query,answer_query
        from tests.test_telegram import update
        payload=update(update_id=uuid4().int % 1000000000000,content='dame una lista de todos los proyectos que tienes')
        with Session(self.engine) as session:
            result=ingest_update(session,payload,12345,is_query=True)
            answer=answer_query(session,catalog_query(payload['message']['text']))
            self.assertIn('Proyectos registrados:',answer)
            self.assertIn('SIMA',answer)
        sid=UUID(result['source_id'])
        with Session(self.engine) as session:
            self.assertEqual(session.get(Source,sid).source_type,'telegram_query')
            self.assertEqual(session.scalar(select(func.count(Task.id)).where(Task.source_id==sid)),0)
            self.assertEqual(session.scalar(select(func.count(ProjectMemoryEvent.id)).where(ProjectMemoryEvent.source_id==sid)),0)

    def test_queue_advisory_lock_excludes_other_backend_on_native_postgres(self):
        if make_url(TEST_URL).port==55439:
            self.skipTest('Embedded database shares one backend; native concurrency must be checked separately')
        from app.services.source_processing_jobs import lock_id
        result,_,_=self.save_document(); key=lock_id(result['source_id'])
        with self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as first, self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as second:
            self.assertTrue(first.scalar(text('SELECT pg_try_advisory_lock(:key)'),{'key':key}))
            try:
                self.assertFalse(second.scalar(text('SELECT pg_try_advisory_lock(:key)'),{'key':key}))
            finally:
                first.scalar(text('SELECT pg_advisory_unlock(:key)'),{'key':key})
            self.assertTrue(second.scalar(text('SELECT pg_try_advisory_lock(:key)'),{'key':key}))
            second.scalar(text('SELECT pg_advisory_unlock(:key)'),{'key':key})
    def test_document_worker_hierarchical_retry_preserves_parts_and_original(self):
        from app.source_processing_worker import run_once,retry_source
        from app.models import DocumentAsset,ProcessingRunPart,SourceProcessingJob
        content='SIMA. '+ 'x'*5880 + ' '+ QUOTE + ' '+ 'y'*31000
        result,_,cfg=self.save_document(caption='/reunion SIMA',content=content)
        sid=UUID(result['source_id']); provider=FakeClaude(fail_part=3)
        with patch('app.source_processing_worker.candidate_ids',return_value=[sid]), patch(
            'app.services.hierarchical_llm.call_json',side_effect=provider), patch('app.source_processing_worker.notify_completed') as notify:
            self.assertEqual(run_once(self.engine,cfg),1)
            notify.assert_not_called()
            with Session(self.engine) as session:
                original_parts=set(session.scalars(select(ProcessingRunPart.id).where(ProcessingRunPart.source_id==sid)))
                self.assertEqual(len(original_parts),2)
                self.assertEqual(session.scalar(select(SourceProcessingJob.status).where(SourceProcessingJob.source_id==sid)),'retry')
                self.assertEqual(session.get(Source,sid).raw_content,content)
            self.assertTrue(retry_source(self.engine,sid))
            provider.fail_part=None; previous_calls=provider.part_calls
            self.assertEqual(run_once(self.engine,cfg),1)
            notify.assert_called_once()
        with Session(self.engine) as session:
            parts=set(session.scalars(select(ProcessingRunPart.id).where(ProcessingRunPart.source_id==sid)))
            self.assertTrue(original_parts.issubset(parts))
            self.assertEqual(provider.part_calls-previous_calls,len(parts)-2)
            self.assertEqual(provider.consolidation_calls,1)
            self.assertEqual(session.scalar(select(SourceProcessingJob.status).where(SourceProcessingJob.source_id==sid)),'completed')
            self.assertEqual(session.scalar(select(DocumentAsset.original_bytes).where(DocumentAsset.source_id==sid)),content.encode())
            self.assertEqual(session.scalar(select(func.count(Task.id)).where(Task.source_id==sid)),1)
            self.assertGreater(session.scalar(select(func.count(TaskEvidence.id)).join(Task).where(Task.source_id==sid)),0)

    def test_queue_workers_do_not_process_same_source_concurrently_on_native_postgres(self):
        if make_url(TEST_URL).port==55439:
            self.skipTest('Native independent backends are required for concurrent workers')
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from app.source_processing_worker import run_once
        result,_,cfg=self.save_document(); sid=UUID(result['source_id'])
        entered=threading.Event(); release=threading.Event()
        def index(*args):
            entered.set()
            if not release.wait(10): raise RuntimeError('Test synchronization timeout')
        with patch('app.source_processing_worker.candidate_ids',return_value=[sid]), patch(
            'app.source_processing_worker.index_source',side_effect=index) as indexed, patch(
            'app.source_processing_worker.process_source',return_value={'status':'processed','tasks_count':0,'decisions_count':0}) as process, patch(
            'app.source_processing_worker.notify_completed'), ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(run_once,self.engine,cfg)
            try:
                self.assertTrue(entered.wait(10))
                self.assertEqual(pool.submit(run_once,self.engine,cfg).result(timeout=10),0)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10),1)
            indexed.assert_called_once(); process.assert_called_once()
