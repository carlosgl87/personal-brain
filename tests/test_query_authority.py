"""Authority, exact rendering, bounded conversational context, and natural follow-ups."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Row
from unittest.mock import MagicMock, patch
from uuid import uuid4
from sqlalchemy.dialects import postgresql
from pydantic import ValidationError
from app.models import Task, Decision, Project, Company, Area, ReasoningRun, Source, TaskChange
from app.schemas.reasoning import QueryPlan, SynthesizedAnswer
from app.services.reasoning import answer_reasoning, retrieve, trace_context
from app.services.structured_queries import render_structured, display_tasks
from app.services.conversation_context import recent_task_response
from app.services.claude import ExtractionError
from test_hierarchical import config
from test_action_plan_v2 import V2Session, proposal, scope

NOW = datetime.now(timezone.utc)


def task(title, status='open', project_id=None):
    return Row(id=uuid4(), project_id=project_id or uuid4(), source_id=uuid4(), processing_run_id=None,
        title=title, description=None, status=status, owner_text=None, due_at=None,
        completed_at=NOW if status=='completed' else None, created_at=NOW, updated_at=NOW)


def exact(**values):
    return QueryPlan(data_authority='structured', presentation='list', time_basis='none',
        include_tasks=True, include_decisions=False, include_project_memory=False,
        include_updates=False, include_recent_sources=False, **values)


class QuerySession:
    """SQL fixture checks predicates; actual SQL tests live in the opt-in PostgreSQL suite."""
    def __init__(self, tasks=(), decisions=(), total=None):
        self.tasks=list(tasks); self.decisions=list(decisions); self.total=total; self.statements=[]

    def execute(self, statement):
        self.statements.append(statement)
        entity=statement.column_descriptions[0]['entity']
        params=statement.compile(dialect=postgresql.dialect()).params
        if entity is Task:
            rows=[t for t in self.tasks if t.status in ('open','in_progress') and t.completed_at is None]
            if 'project_id_1' in params: rows=[t for t in rows if t.project_id in params['project_id_1']]
            total=self.total if self.total is not None else len(rows)
            limit=statement._limit_clause.value
            return Row(all=lambda:[(t,'Catu',total) for t in rows[:limit]])
        if entity is Decision:
            return Row(all=lambda:[(d,'SIMA',len(self.decisions)) for d in self.decisions])
        return Row(all=lambda:[])


class QueryAuthorityTests(unittest.TestCase):
    def context(self, tasks, plan=None, settings=None, total=None):
        session=QuerySession(tasks,total=total)
        with patch('app.services.reasoning.plan_scope',return_value=Row(project_ids=None,label='Todos',error=None)):
            context=retrieve(session,plan or exact(),settings or config())
        return session,context

    def test_exact_pending_ignores_completed_and_historical_facts(self):
        a=task('A abierta'); b=task('B enviar correo',status='completed')
        with patch('app.services.reasoning.semantic_search') as semantic, patch('app.services.reasoning.retrieve_project_memories') as memories:
            session,context=self.context([a,b],exact(semantic_queries=['B en mis notas']))
        semantic.assert_not_called(); memories.assert_not_called()
        answer=render_structured(context)
        self.assertIn('A abierta',answer); self.assertNotIn('B enviar correo',answer)
        self.assertFalse(context['updates']); self.assertFalse(context['recent_sources'])
        self.assertEqual(len(session.statements),1)
        sql=str(session.statements[0].compile(dialect=postgresql.dialect()))
        self.assertIn('completed_at IS NULL',sql)
        self.assertIn('latest_processing_run_id',sql)
        self.assertIn('count(tasks.id) OVER',sql)

    def test_stale_project_memory_cannot_resurrect_completed(self):
        with patch('app.services.reasoning.retrieve_project_memories',return_value=([{'open_items':'B pendiente'}],[],[])) as memory:
            _,context=self.context([task('B pendiente','completed')])
        memory.assert_not_called()
        self.assertEqual(context['coverage']['tasks']['total'],0)
        self.assertNotIn('B pendiente',render_structured(context))

    def test_exact_sql_has_no_semantic_warning_when_embeddings_disabled(self):
        _,context=self.context([task('A')])
        self.assertFalse(context['semantic_coverage']['requested'])
        self.assertFalse(context['warnings'])
        self.assertNotIn('semánt',render_structured(context))

    def test_35_tasks_are_exhaustive(self):
        _,context=self.context([task('Task '+str(i)) for i in range(35)])
        self.assertEqual(context['coverage']['tasks'],{'total':35,'shown':35,'complete':True,'reason':None})
        self.assertEqual(render_structured(context).count('• Task '),35)

    def test_row_safety_limit_reports_total_and_prefix(self):
        tasks=[task(str(i)) for i in range(47)]
        with patch('app.services.structured_queries.ROW_LIMIT',30): _,context=self.context(tasks)
        self.assertIn('Hay 47 tareas abiertas. Te muestro las primeras 30.',render_structured(context))
        self.assertFalse(context['coverage']['tasks']['complete'])

    def test_character_budget_reports_partial(self):
        _,context=self.context([task('x'*500) for _ in range(35)],settings=config(REASONING_CONTEXT_MAX_CHARS=5000))
        self.assertEqual(context['coverage']['tasks']['total'],35)
        self.assertLess(context['coverage']['tasks']['shown'],35)
        self.assertIn('Te muestro las primeras',render_structured(context))
        self.assertLessEqual(len(json.dumps(context,ensure_ascii=False)),5000)

    def test_owner_date_null_never_creates_metadata_noise(self):
        _,context=self.context([task('Revisar crédito')]); answer=render_structured(context)
        for noise in ('sin responsable','sin fecha','source','UUID','metadata'): self.assertNotIn(noise,answer)
        self.assertNotIn(str(context['tasks'][0]['task_id']),answer)

    def test_existing_owner_due_date_are_rendered(self):
        t=task('Revisar');t.owner_text='Ana';t.due_at=NOW
        _,context=self.context([t]); answer=render_structured(context)
        self.assertIn('Ana',answer);self.assertIn('Vence',answer)

    def test_grouping_preserves_display_ordinals(self):
        _,context=self.context([task('Correo'),task('Proactivas')])
        shown=display_tasks(context['tasks'])
        self.assertEqual([x['ordinal'] for x in shown],[1,2])
        self.assertIn('Catu\n',render_structured(context))
        self.assertTrue(all(x['status']=='open' for x in shown))

    def test_decisions_only_uses_decisions_sql(self):
        d=Row(id=uuid4(),decision_text='Usar Databricks',source_id=uuid4(),processing_run_id=None,project_id=uuid4(),decided_at=None)
        s=QuerySession(decisions=[d])
        plan=QueryPlan(data_authority='structured',presentation='list',include_tasks=False,include_decisions=True)
        with patch('app.services.reasoning.plan_scope',return_value=Row(project_ids=None,label='SIMA',error=None)):
            context=retrieve(s,plan,config())
        self.assertEqual(len(s.statements),1)
        self.assertIn('Usar Databricks',render_structured(context))

    def test_catalog_uses_catalog_tables_without_synthesis(self):
        for kind,entity in [('projects',Project),('companies',Company),('areas',Area)]:
            s=MagicMock();s.execute.return_value.all.return_value=[(Row(id=uuid4(),name='Registrado',status='active'),1)]
            p=QueryPlan(data_authority='structured',presentation='list',catalog_entity=kind,include_tasks=False,include_decisions=False)
            with patch('app.services.reasoning.plan_scope',return_value=Row(project_ids=None,label='Todos',error=None)):
                c=retrieve(s,p,config())
            self.assertIs(s.execute.call_args.args[0].column_descriptions[0]['entity'],entity)
            self.assertIn('Registrado',render_structured(c))

    def answer_session(self):
        s=MagicMock(); sid=uuid4()
        s.scalar.side_effect=[Row(id=sid,source_type='telegram_query'),None]
        return s,sid

    def test_simple_exact_answer_skips_second_llm_and_records_shown_tasks(self):
        _,context=self.context([task('Correo')]);s,sid=self.answer_session()
        with patch('app.services.reasoning.retrieve',return_value=context),patch('app.services.reasoning.synthesize') as synth:
            answer=answer_reasoning(s,'Dame todos mis pendientes',sid,config(),interpreted_plan=exact())
        synth.assert_not_called(); self.assertIn('Correo',answer)
        run=s.add.call_args.args[0]
        self.assertEqual(run.retrieved_context['shown_tasks'][0]['title'],'Correo')
        self.assertEqual(run.retrieved_context['shown_tasks'][0]['ordinal'],1)

    def test_exact_analysis_synthesizes_only_sql_evidence(self):
        p=exact();p.presentation='analysis'
        _,context=self.context([task('Correo')],p);s,sid=self.answer_session()
        with patch('app.services.reasoning.retrieve',return_value=context),patch('app.services.reasoning.synthesize',return_value='Primero correo') as synth:
            self.assertEqual(answer_reasoning(s,'Prioriza mis pendientes',sid,config(),interpreted_plan=p),'Primero correo')
        evidence=synth.call_args.args[1]
        self.assertFalse(evidence['chunks']);self.assertFalse(evidence['project_memories'])

    def test_warning_is_typed_and_not_selected_by_words(self):
        _,context=self.context([task('Correo')]);context['warnings']=['embeddings indexados semántica']
        s,sid=self.answer_session()
        with patch('app.services.reasoning.retrieve',return_value=context):
            answer=answer_reasoning(s,'Pendientes',sid,config(),interpreted_plan=exact())
        self.assertNotIn('búsqueda',answer.lower())

    def test_requested_semantic_failure_has_material_warning(self):
        s=MagicMock();s.scalars.return_value.all.return_value=[]
        s.execute.return_value.all.return_value=[]
        p=QueryPlan(semantic_queries=['costos SIMA'],include_tasks=False,include_decisions=False,include_updates=False,include_recent_sources=False,include_project_memory=False)
        with patch('app.services.reasoning.plan_scope',return_value=Row(project_ids=None,label='SIMA',error=None)):
            context=retrieve(s,p,config())
        self.assertEqual(context['semantic_coverage']['status'],'unavailable')
        self.assertTrue(context['semantic_coverage']['material'])
        session,sid=self.answer_session()
        with patch('app.services.reasoning.retrieve',return_value=context):
            answer=answer_reasoning(session,'Qué costos discutimos',sid,config(),interpreted_plan=p)
        self.assertIn('puede omitir temas',answer)

    def test_contextual_exploration_receives_history_and_sql_closed_states(self):
        s=MagicMock();a=task('Pendiente registrado');b=task('Correo antiguo','completed')
        s.scalars.side_effect=[Row(all=lambda:[]),Row(all=lambda:[a,b])]
        source=Row(id=uuid4(),received_at=NOW,raw_content='Compromiso no registrado',source_type='telegram_text')
        s.execute.side_effect=[Row(all=lambda:[(a,'Catu')]),Row(all=lambda:[(source,None)])]
        p=QueryPlan(include_decisions=False,include_updates=False,include_project_memory=False)
        with patch('app.services.reasoning.plan_scope',return_value=Row(project_ids=None,label='Todos',error=None)):
            context=retrieve(s,p,config())
        self.assertEqual(context['recent_sources'][0]['excerpt']['text'],'Compromiso no registrado')
        self.assertEqual({t['status'] for t in context['task_states']},{'open','completed'})
        from app.services.reasoning_llm import SYNTHESIS_SYSTEM, INFORMATION_POLICY
        self.assertIn('Posibles compromisos',SYNTHESIS_SYSTEM)
        self.assertIn('completed/cancelled',INFORMATION_POLICY.lower())

    def test_legacy_query_plan_remains_readable(self):
        p=QueryPlan.model_validate({'scope_type':'project','scope_value':'SIMA'})
        self.assertEqual(p.data_authority,'contextual');self.assertEqual(p.presentation,'analysis')
        with self.assertRaises(ValidationError): QueryPlan(data_authority='keyword_router')

    def test_analytical_display_ids_are_validated(self):
        from app.services.reasoning_llm import synthesize, ReasoningError
        _,context=self.context([task('Correo')])
        tid=context['tasks'][0]['task_id']; sid=context['tasks'][0]['source_id']; shown=[]
        with patch('app.services.reasoning_llm.call_json',return_value=SynthesizedAnswer(text='Correo',source_ids=[sid],shown_task_ids=[tid])):
            synthesize('Prioriza',context,config(),shown_tasks=shown)
        self.assertEqual(shown[0]['task_id'],tid)
        with patch('app.services.reasoning_llm.call_json',return_value=SynthesizedAnswer(text='Inventada',source_ids=[sid],shown_task_ids=[uuid4()])):
            with self.assertRaises(ReasoningError): synthesize('Prioriza',context,config())

    def test_conversation_context_scope_recency_budget_sql(self):
        from app.services.conversation_context import MAX_CONTEXT_TASKS
        s=MagicMock(); source=Row(id=uuid4(),received_at=NOW,raw_metadata={'message':{'chat':{'id':123,'type':'private'},'from':{'id':123}}})
        tasks=[{'task_id':str(uuid4()),'title':'T','project_id':str(uuid4()),'project_name':'Catu','status':'open','ordinal':i+1} for i in range(105)]
        s.scalar.return_value=Row(created_at=NOW-timedelta(minutes=1),question_source_id=uuid4(),retrieved_context={'shown_tasks':tasks})
        result=recent_task_response(s,source)
        self.assertLessEqual(len(result['tasks']),MAX_CONTEXT_TASKS);self.assertGreater(len(result['tasks']),0);self.assertFalse(result['complete'])
        sql=s.scalar.call_args.args[0].compile(dialect=postgresql.dialect())
        self.assertIn('from',str(sql.params));self.assertIn('chat',str(sql.params))
        self.assertIn(NOW-timedelta(hours=6),sql.params.values())
        self.assertIn(NOW,sql.params.values())

    def test_nonprivate_sources_have_no_conversation_context(self):
        s=MagicMock()
        for metadata in ({},{'message':{'chat':{'id':123,'type':'group'},'from':{'id':123}}}):
            self.assertIsNone(recent_task_response(s,Row(raw_metadata=metadata)))
        s.scalar.assert_not_called()

    def test_follow_up_gets_previous_ids_and_refreshed_candidates(self):
        from app.services.action_context import assemble_v2_context
        session=V2Session('Ya completé la de enviar el correo.')
        t=session.pending_in(session.projects[1],'Enviar correo')
        previous={'tasks':[{'task_id':str(t.id),'title':t.title,'project_id':str(t.project_id),'project_name':'Catu','status':'open','ordinal':1,'project_ordinal':1}], 'complete':True}
        with session.begin(),patch('app.services.conversation_context.recent_task_response',return_value=previous):
            context=assemble_v2_context(session,session.source,session.projects,config())
        self.assertTrue(context['recent_task_response']['tasks'][0]['currently_open_candidate'])
        self.assertIn(str(t.project_id),[g['project_id'] for g in context['candidate_projects']])

    def test_follow_up_completion_uses_action_plan_v2(self):
        from app.services.processing import process_text_source
        s=V2Session('Ya completé la de enviar el correo.');t=s.pending_in(s.projects[1],'Enviar correo');other=s.pending_in(s.projects[1],'Proactivas')
        previous={'tasks':[{'task_id':str(t.id),'title':t.title,'project_id':str(t.project_id),'project_name':'Catu','status':'open','ordinal':1}], 'complete':True}
        def interpret(settings,context):
            self.assertEqual(context['recent_task_response']['tasks'][0]['task_id'],str(t.id))
            return proposal(completed_tasks=[scope(s.projects[1],task_id=t.id,confidence=.99,state='performed',alternatives=[],evidence=s.source.raw_content)])
        with patch('app.services.conversation_context.recent_task_response',return_value=previous),patch('app.services.message_interpreter.interpret',side_effect=interpret),patch('app.services.action_execution_v2.mark_dirty'),patch('app.services.task_management.mark_dirty'):
            result=process_text_source(s,s.source.id,config())
        self.assertEqual(t.status,'completed');self.assertEqual(other.status,'open')
        self.assertEqual(result['execution']['items'][0]['task_id'],str(t.id))

    def test_ambiguous_follow_up_never_executes(self):
        from app.services.processing import process_text_source
        from app.services.message_handling import respond_to_plan
        s=V2Session('Ya hice esa.');a=s.pending_in(s.projects[1],'Enviar correo A');b=s.pending_in(s.projects[1],'Enviar correo B')
        previous={'tasks':[{'task_id':str(t.id),'title':t.title,'project_id':str(t.project_id),'status':'open','ordinal':i+1} for i,t in enumerate([a,b])],'complete':True}
        p=proposal(completed_tasks=[scope(s.projects[1],task_id=a.id,confidence=.99,state='performed',alternatives=[b.id],evidence=s.source.raw_content)])
        with patch('app.services.conversation_context.recent_task_response',return_value=previous),patch('app.services.message_interpreter.interpret',return_value=p):
            result=process_text_source(s,s.source.id,config())
        self.assertEqual([a.status,b.status],['open','open'])
        self.assertIn('Confirma el pendiente',respond_to_plan(s,result,config()))

    def test_historical_response_and_task_are_not_rewritten(self):
        s=MagicMock();metadata={'shown_tasks':[{'task_id':str(uuid4()),'title':'T','project_id':str(uuid4()),'status':'open','ordinal':1}]}
        original=copy.deepcopy(metadata)
        s.scalar.return_value=Row(created_at=NOW,question_source_id=uuid4(),retrieved_context=metadata)
        source=Row(id=uuid4(),received_at=NOW,raw_metadata={'message':{'chat':{'id':1,'type':'private'},'from':{'id':1}}})
        result=recent_task_response(s,source)
        result['tasks'][0]['currently_open_candidate']=True
        self.assertEqual(metadata,original)


    def test_empty_semantic_result_is_not_itself_incomplete_coverage(self):
        s=MagicMock();s.scalars.return_value.all.return_value=[];s.execute.return_value.all.return_value=[]
        p=QueryPlan(semantic_queries=['costos'],include_tasks=False,include_decisions=False,include_updates=False,include_recent_sources=False,include_project_memory=False)
        cfg=config(OPENROUTER_API_KEY='fake',OPENROUTER_EMBEDDING_MODEL='test',OPENROUTER_EMBEDDING_DIMENSIONS=3)
        with patch('app.services.reasoning.plan_scope',return_value=Row(project_ids=None,label='Todos',error=None)),patch(
                'app.services.semantic_coverage.semantic_index_incomplete',return_value=False),patch('app.services.reasoning.semantic_search',return_value=[]):
            context=retrieve(s,p,cfg)
        self.assertEqual(context['semantic_coverage']['status'],'no_results')
        self.assertFalse(context['semantic_coverage']['material'])

    def test_unindexed_notes_mark_material_search_limit_only_in_scope(self):
        from app.services.semantic_coverage import semantic_index_incomplete
        from app.services.queries import Scope
        s=MagicMock();s.scalar.return_value=uuid4();pid=uuid4()
        self.assertTrue(semantic_index_incomplete(s,Scope([pid],'Catu'),config(),QueryPlan()))
        sql=s.scalar.call_args.args[0].compile(dialect=postgresql.dialect())
        self.assertIn('latest_processing_run_id',str(sql));self.assertIn(str(pid),str(sql.params))
        self.assertIn('openrouter',str(sql.params));self.assertIn('chunk_embeddings',str(sql))
