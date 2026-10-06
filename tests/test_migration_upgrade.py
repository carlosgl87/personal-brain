"""Upgrade a disposable EMPTY database through old data to the current release."""
import os
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
            command.upgrade(cfg,'head')
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
            self.assertEqual(session.scalar(select(func.count(Project.id))),32)
        engine.dispose()
