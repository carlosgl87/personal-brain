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
from app.models import TaskCompletionAttempt
from app.models import ProjectUpdate, UpdateEvidence, ProcessingRunPart
from app.schemas.action_plan import ActionPlan
from app.services.task_completion import CompletionProposal, complete_from_note
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
            if conn.scalar(text('SELECT version_num FROM alembic_version')) != '0012_action_plans':
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

    def test_action_plan_multiple_changes_and_updates_commit_once(self):
        from test_action_plans import plan, completion
        original = self.save_source(self.a)
        first, second = uuid4(), uuid4()
        with Session(self.engine) as session, session.begin():
            session.add_all([Task(id=first, source_id=original, project_id=self.a, title='Enviar correos', status='open'),
                             Task(id=second, source_id=original, project_id=self.a, title='Terminar presentación', status='open')])
        quote = 'Ya envié los correos y terminé la presentación.'
        note = self.save_source(content=quote, source_type='telegram_text', metadata={'processing_schema': 'action-plan-v1'})
        with Session(self.engine) as session:
            proposal = plan(self.a, completed_tasks=[completion(session.get(Task, tid), quote) for tid in (first, second)],
                            updates=[{'update_text': 'Correos enviados y presentación terminada.', 'evidence': quote}])
        with patch('app.services.message_interpreter.interpret', return_value=proposal) as llm:
            with Session(self.engine) as session: process_text_source(session, note, self.cfg)
            with Session(self.engine) as session: process_text_source(session, note, self.cfg)
        llm.assert_called_once()
        with Session(self.engine) as session:
            changes = session.scalars(select(TaskChange).where(TaskChange.command_source_id == note)).all()
            self.assertEqual(len(changes), 2)
            self.assertEqual({c.task_id for c in changes}, {first, second})
            self.assertTrue(all(c.action == 'natural_completion' for c in changes))
            self.assertEqual(session.scalar(select(func.count(ProjectUpdate.id)).where(ProjectUpdate.source_id == note)), 1)
            self.assertEqual(session.scalar(select(func.count(TaskCompletionAttempt.id)).where(TaskCompletionAttempt.source_id == note)), 0)
            self.assertTrue(session.get(ProjectMemoryState, self.a).is_dirty)
            self.assertEqual(session.get(Source, note).raw_content, quote)
        with self.assertRaises(DBAPIError), Session(self.engine) as session, session.begin():
            session.add(TaskChange(task_id=first, command_source_id=note, action='natural_completion', before={}, after={}, answer='duplicate'))
            session.flush()

    def test_updates_and_evidence_foreign_keys_offsets_and_immutability(self):
        source_id = self.save_source(self.a, 'Datos validados.')
        run_id, update_id, chunk_id, part_id = uuid4(), uuid4(), uuid4(), uuid4()
        with Session(self.engine) as session, session.begin():
            session.add(ProcessingRun(id=run_id, source_id=source_id, provider='anthropic', model='test',
                                     prompt_version='message-interpreter-v1', result={'schema_version': 'action-plan-v1'}))
            session.add(SourceChunk(id=chunk_id, source_id=source_id, chunk_version='test', chunk_index=0,
                                    content='Datos validados.', char_start=0, char_end=16, chunk_metadata={}))
            session.flush()
            session.add(ProcessingRunPart(id=part_id, source_id=source_id, source_chunk_id=chunk_id,
                chunk_version='test', part_index=0, provider='anthropic', model='test', prompt_version='action-plan-part-v1', result={}))
            session.add(ProjectUpdate(id=update_id, source_id=source_id, processing_run_id=run_id,
                                      project_id=self.a, update_text='Datos validados.'))
            session.flush()
            session.add(UpdateEvidence(update_id=update_id, source_chunk_id=chunk_id, processing_run_part_id=part_id,
                                       evidence='Datos validados.', char_start=0, char_end=16))
        for statement in ["UPDATE project_updates SET update_text='changed' WHERE id=:id", 'DELETE FROM project_updates WHERE id=:id']:
            with self.assertRaises(DBAPIError), self.engine.begin() as conn:
                conn.execute(text(statement), {'id': update_id})
        with self.assertRaises(DBAPIError), Session(self.engine) as session, session.begin():
            session.add(ProjectUpdate(source_id=uuid4(), processing_run_id=run_id, project_id=self.a, update_text='bad FK'))
            session.flush()
        with self.assertRaises(DBAPIError), Session(self.engine) as session, session.begin():
            session.add(UpdateEvidence(update_id=update_id, source_chunk_id=chunk_id, processing_run_part_id=part_id,
                                       evidence='bad offsets', char_start=10, char_end=5))
            session.flush()
        with Session(self.engine) as session:
            update = session.get(ProjectUpdate, update_id)
            self.assertEqual(update.update_text, 'Datos validados.')
            evidence = session.scalar(select(UpdateEvidence).where(UpdateEvidence.update_id == update_id))
            self.assertEqual(session.get(Source, source_id).raw_content[evidence.char_start:evidence.char_end], evidence.evidence)

    def natural_completion_fixture(self):
        original = self.save_source(self.a)
        self.process(original)
        with Session(self.engine) as session, session.begin():
            task = session.scalar(select(Task).where(Task.source_id == original))
            task_id = task.id
            note_id = uuid4()
            note = 'Ya envié el informe del proyecto Audit ' + str(self.a) + '.'
            session.add(Source(id=note_id, source_type='telegram_text', raw_content=note,
                raw_metadata={}, external_source='telegram', external_id=str(note_id), primary_project_id=self.a))
        proposal = CompletionProposal(completion_detected=True, matched_task_id=task_id,
            confidence=.99, reason_code='completed', evidence=note, alternatives=[])
        return original, note_id, task_id, note, proposal

    def test_natural_completion_persists_once_and_flows_into_memory_version(self):
        original, note_id, task_id, note, proposal = self.natural_completion_fixture()
        with Session(self.engine) as session, session.begin():
            captured, data = snapshot_data(session, self.a, self.cfg)
            publish_memory(session, self.a, captured, data, memory(original).model_dump(mode='json'),
                           self.cfg, False, self.now)
        with patch('app.services.task_completion.propose_completion', return_value=proposal) as llm:
            with Session(self.engine) as session:
                first = complete_from_note(session, note_id, self.cfg)
            with Session(self.engine) as session:
                second = complete_from_note(session, note_id, self.cfg)
        self.assertEqual(first, second)
        llm.assert_called_once()
        with Session(self.engine) as session, session.begin():
            self.assertEqual(session.get(Task, task_id).status, 'completed')
            self.assertEqual(session.get(Source, note_id).raw_content, note)
            self.assertEqual(session.scalar(select(func.count(TaskChange.id)).where(
                TaskChange.command_source_id == note_id)), 1)
            self.assertEqual(session.scalar(select(func.count(TaskCompletionAttempt.id)).where(
                TaskCompletionAttempt.source_id == note_id)), 1)
            self.assertEqual(session.scalar(select(func.count(ProjectMemoryEvent.id)).where(
                ProjectMemoryEvent.command_source_id == note_id)), 1)
            _, deltas, _ = retrieve_project_memories(session, Scope([self.a], 'A'), self.cfg)
            delta = next(d for d in deltas if d.get('source_id') == str(note_id))
            self.assertEqual(delta['action'], 'natural_completion')
            self.assertEqual(delta['current_task']['status'], 'completed')
            captured, data = snapshot_data(session, self.a, self.cfg)
            self.assertEqual(data['changes'][0]['action'], 'natural_completion')
            version_id = publish_memory(session, self.a, captured, data,
                memory(note_id, 'Informe enviado.').model_dump(mode='json'), self.cfg, False, self.now)
            self.assertEqual(session.get(ProjectMemoryVersion, version_id).through_revision, captured.cursor)
            self.assertFalse(session.get(ProjectMemoryState, self.a).is_dirty)

    def test_natural_completion_rolls_back_task_audit_and_attempt_when_event_fails(self):
        _, note_id, task_id, note, proposal = self.natural_completion_fixture()
        with Session(self.engine) as session, patch('app.services.task_completion.propose_completion', return_value=proposal), patch(
                'app.services.task_management.mark_dirty', side_effect=RuntimeError('Simulated event failure')):
            with self.assertRaises(RuntimeError):
                complete_from_note(session, note_id, self.cfg)
        with Session(self.engine) as session:
            self.assertEqual(session.get(Task, task_id).status, 'open')
            self.assertEqual(session.get(Source, note_id).raw_content, note)
            self.assertEqual(session.scalar(select(func.count(TaskChange.id)).where(
                TaskChange.command_source_id == note_id)), 0)
            self.assertEqual(session.scalar(select(func.count(TaskCompletionAttempt.id)).where(
                TaskCompletionAttempt.source_id == note_id)), 0)

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
        result,_,cfg=self.save_document(content=QUOTE); sid=UUID(result['source_id'])
        with Session(self.engine) as session:
            pid = session.get(Source, sid).primary_project_id
        from app.schemas.action_plan import ActionPlanV2
        item = output(task=True)['tasks'][0] | {'project_id': pid, 'scope_confidence': .99}
        v2 = ActionPlanV2(schema_version='action-plan-v2', interaction='update', summary='Informe', tasks=[item])
        with patch('app.source_processing_worker.candidate_ids',return_value=[sid]), patch(
            'app.services.message_interpreter.interpret',return_value=v2) as extract, patch(
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
        from app.schemas.action_plan import ActionPlanV2
        quote = 'Ana debe enviar el informe mañana.'
        proposal = ActionPlanV2(schema_version='action-plan-v2', interaction='update', summary='Informe', tasks=[{'project_id': sima, 'scope_confidence': .99, 'title': 'Enviar informe', 'description': None, 'owner_text': 'Ana', 'due_at': None, 'evidence': quote}])
        with Session(self.engine) as session,patch('app.services.message_interpreter.interpret',return_value=proposal):
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
        from app.schemas.hierarchical import ConsolidatedActionPlanV2
        base_provider = provider
        def v2_provider(settings, system, data, schema, client=None, **kwargs):
            result = base_provider(settings, system, data, schema, client, **kwargs)
            if schema is ConsolidatedActionPlanV2:
                payload = result.model_dump(mode='json')
                pid = payload.pop('project_id')
                for kind in ('tasks', 'decisions', 'updates'):
                    for item in payload[kind]: item.update(project_id=pid, scope_confidence=.99, ambiguities=[])
                payload.update(schema_version='action-plan-v2', interaction='update')
                return ConsolidatedActionPlanV2.model_validate(payload)
            return result
        with patch('app.source_processing_worker.candidate_ids',return_value=[sid]), patch(
            'app.services.hierarchical_llm.call_json',side_effect=v2_provider), patch('app.source_processing_worker.notify_completed') as notify:
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


    def test_v2_multi_project_membership_memory_and_idempotency(self):
        from app.schemas.action_plan import ActionPlanV2
        from app.services.action_context import assemble_v2_context
        quote = 'Ya envié accesos y terminé la presentación.'
        note = self.save_source(content=quote, source_type='telegram_text', metadata={'processing_schema': 'action-plan-v2'})
        first, second = uuid4(), uuid4()
        with Session(self.engine) as session, session.begin():
            session.add_all([Task(id=first, project_id=self.a, title='Enviar accesos', status='open'),
                             Task(id=second, project_id=self.b, title='Presentación', status='open')])
        # Fixture names are explicit in source text so local retrieval finds both.
        with Session(self.engine) as session:
            names = [session.get(Project, pid).name for pid in (self.a, self.b)]
        # Sources are immutable; create the actual note with names rather than updating it.
        note = self.save_source(content=' / '.join(names) + ': ' + quote, source_type='telegram_text', metadata={'processing_schema': 'action-plan-v2'})
        proposal = ActionPlanV2(schema_version='action-plan-v2', interaction='update', summary='Accesos y presentación',
            completed_tasks=[{'project_id': pid, 'scope_confidence': .99, 'task_id': tid, 'confidence': .99,
                'state': 'performed', 'evidence': quote, 'alternatives': []} for pid, tid in ((self.a, first), (self.b, second))],
            updates=[{'project_id': pid, 'scope_confidence': .99, 'update_text': 'Trabajo terminado', 'evidence': quote} for pid in (self.a, self.b)])
        with patch('app.services.message_interpreter.interpret', return_value=proposal) as llm:
            with Session(self.engine) as session: process_text_source(session, note, self.cfg)
            with Session(self.engine) as session: process_text_source(session, note, self.cfg)
        llm.assert_called_once()
        with Session(self.engine) as session:
            self.assertIsNone(session.get(Source, note).primary_project_id)
            self.assertEqual(session.scalar(select(func.count(TaskChange.id)).where(TaskChange.command_source_id == note)), 2)
            for pid in (self.a, self.b):
                self.assertTrue(session.get(ProjectMemoryState, pid).is_dirty)
                rows = session.execute(source_statement(Scope([pid], 'fixture')).where(Source.id == note)).all()
                self.assertEqual(len(rows), 1)
                _, data = snapshot_data(session, pid, self.cfg)
                self.assertTrue(any(u['source_id'] == str(note) for u in data['updates']))
                self.assertTrue(any(s['source_id'] == str(note) for s in data['sources']))
            self.assertFalse(session.execute(source_statement(Scope([], 'empty')).where(Source.id == note)).all())

    def test_v2_late_persistence_failure_rolls_back_all_projects(self):
        from app.schemas.action_plan import ActionPlanV2
        quote = 'SIMA: validamos los accesos.'
        note = self.save_source(content=quote, source_type='telegram_text', metadata={'processing_schema': 'action-plan-v2'})
        with Session(self.engine) as session:
            pid = session.scalar(select(Project.id).where(Project.slug == 'sima'))
        proposal = ActionPlanV2(schema_version='action-plan-v2', interaction='update', summary='Accesos', updates=[
            {'project_id': pid, 'scope_confidence': .99, 'update_text': 'Accesos validados', 'evidence': quote}])
        with Session(self.engine) as session, patch('app.services.message_interpreter.interpret', return_value=proposal), patch(
                'app.services.action_execution_v2.mark_dirty', side_effect=RuntimeError('rollback fixture')):
            with self.assertRaises(RuntimeError): process_text_source(session, note, self.cfg)
        with Session(self.engine) as session:
            self.assertEqual(session.scalar(select(func.count(ProjectUpdate.id)).where(ProjectUpdate.source_id == note)), 0)
            self.assertEqual(session.scalar(select(func.count(ProcessingRun.id)).where(ProcessingRun.source_id == note)), 0)
            self.assertIsNone(session.get(Source, note).latest_processing_run_id)


    def test_exact_sql_query_excludes_completed_and_history_and_preserves_rows(self):
        from app.schemas.reasoning import QueryPlan
        from app.services.reasoning import answer_reasoning
        original=self.save_source(self.a, 'La Task B estaba pendiente: enviar el correo histórico.')
        question=self.save_source(content='Dame todos mis pendientes.', source_type='telegram_query')
        a,b=uuid4(),uuid4()
        with Session(self.engine) as session,session.begin():
            session.add_all([Task(id=a,source_id=original,project_id=self.a,title='Task A abierta',status='open'),
                Task(id=b,source_id=original,project_id=self.a,title='Task B completada',status='completed',completed_at=self.now)])
            name=session.get(Project,self.a).name
        plan=QueryPlan(data_authority='structured',presentation='list',scope_type='project',scope_value=name,
            include_tasks=True,include_decisions=False,semantic_queries=['el correo histórico'],time_basis='none')
        with Session(self.engine) as session,patch('app.services.reasoning.synthesize') as synth,patch('app.services.reasoning.semantic_search') as semantic:
            answer=answer_reasoning(session,'Dame todos mis pendientes.',question,self.cfg,interpreted_plan=plan)
        synth.assert_not_called();semantic.assert_not_called()
        self.assertIn('Task A abierta',answer);self.assertNotIn('Task B',answer);self.assertNotIn('histórico',answer)
        from app.models import ReasoningRun
        with Session(self.engine) as session:
            run=session.scalar(select(ReasoningRun).where(ReasoningRun.question_source_id==question))
            self.assertEqual(run.retrieved_context['coverage']['tasks']['total'],1)
            self.assertEqual(run.retrieved_context['shown_tasks'][0]['task_id'],str(a))
            self.assertEqual(session.get(Task,b).status,'completed')
            self.assertEqual(session.get(Source,original).raw_content,'La Task B estaba pendiente: enviar el correo histórico.')

    def test_previous_task_response_is_scoped_and_expired_context_is_ignored(self):
        from app.models import ReasoningRun
        from app.services.conversation_context import recent_task_response
        chat=self.a.int % 1000000000
        metadata={'message':{'chat':{'id':chat,'type':'private'},'from':{'id':123}}}
        old=self.save_source(content='Old query',source_type='telegram_query',metadata=metadata)
        current=self.save_source(content='Ya hice esa.',source_type='telegram_text',metadata=metadata)
        unrelated=self.save_source(content='Other chat',source_type='telegram_query',metadata={'message':{'chat':{'id':chat+1,'type':'private'},'from':{'id':123}}})
        tid=uuid4()
        payload={'shown_tasks':[{'task_id':str(tid),'title':'Correo','project_id':str(self.a),'project_name':'Fixture','status':'open','ordinal':1,'project_ordinal':1}]}
        with Session(self.engine) as session,session.begin():
            source=session.get(Source,current)
            stamp=source.received_at
            session.add_all([ReasoningRun(question_source_id=old,provider='test',model='test',planner_version='fixture',plan={},retrieved_context=payload,answer='Correo',created_at=stamp-timedelta(minutes=1)),
                ReasoningRun(question_source_id=unrelated,provider='test',model='test',planner_version='fixture',plan={},retrieved_context={'shown_tasks':[{'task_id':str(uuid4()),'title':'Other'}]},answer='Other',created_at=stamp-timedelta(seconds=1))])
        with Session(self.engine) as session:
            source=session.get(Source,current)
            previous=recent_task_response(session,source)
            self.assertEqual(previous['tasks'][0]['task_id'],str(tid))
            # A detached synthetic clock exercises expiry without updating any Source.
            synthetic=type('SourceClock',(),{'id':source.id,'raw_metadata':source.raw_metadata,'received_at':source.received_at+timedelta(hours=7)})()
            self.assertIsNone(recent_task_response(session,synthetic))
