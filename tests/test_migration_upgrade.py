"""Upgrade a disposable EMPTY database through old data to the current release."""
import os
import json
import unittest
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch
from sqlalchemy import create_engine, select, text, func
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from alembic import command
from alembic.config import Config
from app.config import ROOT
from app.models import Source, SourceChunk, ChunkEmbedding, Project, ProcessingRunPart, ProjectMemoryState
from app.seed import seed

URL=os.environ.get('PERSONAL_BRAIN_MIGRATION_TEST_DATABASE_URL')


@unittest.skipUnless(URL,'Requires an explicitly configured empty disposable local database')
class MigrationUpgradeTests(unittest.TestCase):
    def test_upgrade_preserves_existing_sources_chunks_vectors_and_partials(self):
        url=make_url(URL)
        embedded=url.host=='127.0.0.1' and url.port==55440 and url.database=='postgres'
        if url.host not in {'127.0.0.1','localhost','::1'} or not ((url.database or '').startswith('personal_brain_test_') or embedded):
            raise ValueError('Use an empty disposable loopback database.')
        engine=create_engine(url,poolclass=NullPool,connect_args={'prepare_threshold':None})
        with engine.connect() as conn:
            tables=conn.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"))
            self.assertEqual(tables,0,'Migration fixture must be empty; no tables are deleted by this test.')
        original_factory=create_engine
        def local_factory(*args,**kwargs):
            kwargs.setdefault('connect_args',{})['prepare_threshold']=None
            return original_factory(*args,**kwargs)
        cfg=Config(str(ROOT/'alembic.ini'))
        cfg.set_main_option('script_location',str(ROOT/'migrations'))
        with patch('app.config.get_settings',return_value=SimpleNamespace(database_url=URL)),patch('sqlalchemy.create_engine',side_effect=local_factory):
            command.upgrade(cfg,'0007_hierarchical_extraction')
            source_id,chunk_id,part_id=uuid4(),uuid4(),uuid4()
            original='Legacy source with exact offsets.'
            with Session(engine) as session,session.begin():
                seed(session)
                project_id=session.scalar(select(Project.id).limit(1))
                session.add(Source(id=source_id,source_type='meeting_transcript',raw_content=original,raw_metadata={'fixture':'legacy'},primary_project_id=project_id))
                session.flush()
                session.execute(text('''INSERT INTO source_chunks
                    (id,source_id,chunk_version,chunk_index,content,char_start,char_end,metadata,embedding,embedding_model,embedding_dimensions)
                    VALUES (:id,:source,'legacy-version',0,:content,0,:length,'{}',CAST('[1,0,0]' AS vector),'legacy-model',3)'''),
                    {'id':chunk_id,'source':source_id,'content':original,'length':len(original)})
                session.add(ProcessingRunPart(id=part_id,source_id=source_id,source_chunk_id=chunk_id,chunk_version='legacy-version',part_index=0,
                    provider='anthropic',model='legacy-test',prompt_version='legacy-prompt',result={'summary':'Saved partial'}))
            command.upgrade(cfg,'0011_task_completion_attempts')
            run_id, task_id, decision_id, change_id, attempt_id, version_id = [uuid4() for _ in range(6)]
            with engine.begin() as conn:
                conn.execute(text('''INSERT INTO processing_runs (id,source_id,provider,model,prompt_version,result)
                    VALUES (:id,:source,'anthropic','legacy-test','legacy-prompt',CAST(:result AS jsonb))'''),
                    {'id': run_id, 'source': source_id, 'result': json.dumps({'summary': 'Old result', 'tasks': [], 'decisions': []})})
                conn.execute(text("INSERT INTO tasks (id,source_id,project_id,processing_run_id,title,status) VALUES (:id,:source,:project,:run,'Old task','open')"),
                    {'id': task_id, 'source': source_id, 'project': project_id, 'run': run_id})
                conn.execute(text("INSERT INTO decisions (id,source_id,project_id,processing_run_id,decision_text) VALUES (:id,:source,:project,:run,'Old decision')"),
                    {'id': decision_id, 'source': source_id, 'project': project_id, 'run': run_id})
                conn.execute(text('''INSERT INTO task_changes (id,task_id,command_source_id,action,before,after,answer)
                    VALUES (:id,:task,:source,'completar','{}','{}','Old audit')'''),
                    {'id': change_id, 'task': task_id, 'source': source_id})
                conn.execute(text("INSERT INTO task_completion_attempts (id,source_id,result,answer) VALUES (:id,:source,'{}','Old attempt')"),
                    {'id': attempt_id, 'source': source_id})
                conn.execute(text('''INSERT INTO project_memory_versions
                    (id,project_id,version_number,memory,update_type,trigger_source_ids,retrieved_context,through_revision,
                     consolidated_through_at,provider,model,prompt_version)
                    VALUES (:id,:project,1,'{}','incremental','[]','{}',0,now(),'anthropic','legacy-test','old-memory')'''),
                    {'id': version_id, 'project': project_id})
            preserved_tables = ('sources', 'tasks', 'decisions', 'processing_runs', 'processing_run_parts',
                                'task_changes', 'task_completion_attempts', 'project_memory_versions',
                                'project_memory_state', 'project_memory_events')
            def snapshot():
                with engine.connect() as conn:
                    return {table: conn.execute(text('SELECT to_jsonb(t) FROM ' + table + ' t ORDER BY id')).scalars().all()
                            for table in preserved_tables if table != 'project_memory_state'} | {
                        'project_memory_state': conn.execute(text('SELECT to_jsonb(t) FROM project_memory_state t ORDER BY project_id')).scalars().all()}
            old_rows = snapshot()
            command.upgrade(cfg,'head')
            self.assertEqual(snapshot(), old_rows, 'Action Plan migration must preserve every historical row exactly.')
            # Idempotent repeat: the copied vector must not duplicate or modify text.
            command.upgrade(cfg,'head')
        with Session(engine) as session:
            self.assertEqual(session.get(Source,source_id).raw_content,original)
            chunk=session.get(SourceChunk,chunk_id)
            self.assertEqual(chunk.content,original)
            self.assertEqual(chunk.char_end,len(original))
            self.assertEqual(chunk.chunk_version,'legacy-version')
            self.assertIsNone(chunk.logical_version)
            self.assertEqual(session.get(ProcessingRunPart,part_id).result,{'summary':'Saved partial'})
            vector=session.scalar(select(ChunkEmbedding).where(ChunkEmbedding.source_chunk_id==chunk_id))
            self.assertEqual(vector.model,'legacy-model')
            self.assertEqual(vector.dimensions,3)
            self.assertEqual(list(vector.embedding),[1,0,0])
            self.assertEqual(session.scalar(select(func.count(ProjectMemoryState.project_id))),0)
            self.assertEqual(session.scalar(select(func.count(Project.id))), len(json.loads((ROOT/'app/seed_data.json').read_text(encoding='utf-8'))['projects']))
            self.assertEqual(session.scalar(text('SELECT count(*) FROM project_updates')), 0)
            self.assertEqual(session.scalar(text('SELECT count(*) FROM update_evidence')), 0)
        engine.dispose()
