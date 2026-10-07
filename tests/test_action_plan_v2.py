import copy
import unittest
from types import SimpleNamespace as Row
from unittest.mock import patch
from uuid import uuid4
from sqlalchemy.dialects import postgresql
from app.models import Task, Project, ProjectUpdate, Decision, ProcessingRun, TaskChange
from app.schemas.action_plan import ActionPlanV2, read_action_plan
from app.services.action_context import assemble_v2_context
from app.services.project_candidates import candidate_projects
from app.services.processing import process_text_source, run_result
from app.services.message_handling import respond_to_plan
from app.services.message_interpreter import validate_plan
from app.services.claude import ExtractionError
from test_action_plans import ActionSession, NOW, new_task, plan as v1_plan
from test_hierarchical import config


class V2Session(ActionSession):
    def __init__(self, text='SIMA y Catu: ya envié accesos y terminé presentación.'):
        super().__init__(text)
        self.source.raw_metadata['processing_schema'] = 'action-plan-v2'
        self.projects.append(Row(id=uuid4(), name='Catu', slug='catu', aliases=[], area=None, company=None))

    def scalars(self, statement):
        entity = statement.column_descriptions[0]['entity']
        if entity is Task:
            params = statement.compile(dialect=postgresql.dialect()).params
            rows = [r for r in self.rows if isinstance(r, Task)]
            if 'id_1' in params:
                rows = [r for r in rows if r.id in params['id_1']]
            if 'project_id_1' in params:
                rows = [r for r in rows if r.project_id == params['project_id_1']]
            if statement.column_descriptions[0]['name'] != 'source_id':
                rows = [r for r in rows if r.status in ('open', 'in_progress') and not r.completed_at]
            name = statement.column_descriptions[0]['name']
            if name in {'source_id', 'id'}:
                rows = [getattr(r, name) for r in rows]
            if statement._limit_clause is not None:
                rows = rows[:statement._limit_clause.value]
            return Row(all=lambda: rows)
        return super().scalars(statement)

    def pending_in(self, project, title):
        task = self.pending(title)
        task.project_id = project.id
        return task


def scope(project, **values):
    return {'project_id': project.id, 'scope_confidence': .99, 'ambiguities': []} | values


def completion(project, task, **values):
    return scope(project, task_id=task.id, confidence=.99, evidence='ya envié accesos',
                 state='performed', alternatives=[]) | values


def proposal(**values):
    return ActionPlanV2(schema_version='action-plan-v2', interaction='update', summary='Resumen', **values)


class V2Tests(unittest.TestCase):
    def execute(self, session, plan):
        with patch('app.services.message_interpreter.interpret', return_value=plan), patch(
                'app.services.action_execution_v2.mark_dirty') as dirty, patch(
                'app.services.task_management.mark_dirty'):
            result = process_text_source(session, session.source.id, config())
        self.last_dirty = dirty
        return result

    def test_global_over_100_relevant_project_five_can_complete(self):
        s = V2Session()
        selected = [s.pending_in(s.projects[0], str(i)) for i in range(5)]
        for i in range(150): s.pending_in(s.projects[1], str(i))
        r = self.execute(s, proposal(completed_tasks=[completion(s.projects[0], selected[0])]))
        self.assertEqual(selected[0].status, 'completed')
        self.assertEqual(r['execution']['items'][0]['status'], 'completed')

    def test_over_100_in_one_project_cannot_complete(self):
        s = V2Session()
        tasks = [s.pending(str(i)) for i in range(101)]
        r = self.execute(s, proposal(completed_tasks=[completion(s.projects[0], tasks[0])]))
        self.assertEqual(tasks[0].status, 'open')
        self.assertEqual(r['execution']['items'][0]['reason'], 'incomplete_candidates')

    def test_500_projects_do_not_fail(self):
        s = V2Session('Catu: implementar crédito.')
        s.projects.extend(Row(id=uuid4(), name='Proyecto ' + str(i), slug='p'+str(i), aliases=[], area=None, company=None) for i in range(500))
        with s.begin(): context = assemble_v2_context(s, s.source, s.projects, config())
        self.assertEqual([p['name'] for p in context['projects']], ['Catu'])

    def test_alias_shortlist(self):
        s = V2Session('Agente vendedor: revisar crédito.')
        s.projects[1].aliases = [Row(alias='Agente vendedor')]
        candidates, truncated = candidate_projects(s.projects, s.source.raw_content)
        self.assertIn(str(s.projects[1].id), [p['id'] for p in candidates])
        self.assertFalse(truncated)

    def test_ambiguous_alias_keeps_both(self):
        s = V2Session('Hablé con McKinsey.')
        for p in s.projects: p.aliases = [Row(alias='McKinsey')]
        candidates, _ = candidate_projects(s.projects, s.source.raw_content)
        self.assertEqual(len(candidates), 2)

    def test_budget_does_not_hide_equal_rank_alternatives(self):
        s = V2Session('McKinsey')
        for p in s.projects: p.aliases = [Row(alias='McKinsey')]
        candidates, truncated = candidate_projects(s.projects, s.source.raw_content, max_chars=200)
        self.assertTrue(truncated)
        self.assertEqual(candidates, [])

    def assert_multi(self, kind, fields, klass):
        s = V2Session()
        r = self.execute(s, proposal(**{kind: [scope(p, evidence='ya envié accesos', **fields) for p in s.projects]}))
        rows = [row for row in s.rows if isinstance(row, klass)]
        self.assertEqual({row.project_id for row in rows}, {p.id for p in s.projects})
        self.assertIsNone(s.source.primary_project_id)
        self.assertEqual(len(r['execution']['items']), 2)
        self.assertEqual({c.args[1] for c in self.last_dirty.call_args_list}, {p.id for p in s.projects})
        return s, r

    def test_multi_project_updates(self):
        self.assert_multi('updates', {'update_text': 'Se enviaron accesos.'}, ProjectUpdate)

    def test_multi_project_tasks(self):
        self.assert_multi('tasks', {'title': 'Revisar acceso', 'description': None, 'owner_text': None, 'due_at': None}, Task)

    def test_multi_project_decisions(self):
        self.assert_multi('decisions', {'decision_text': 'Usaremos un agente.', 'decided_at': None}, Decision)

    def test_multi_project_completions(self):
        s = V2Session()
        tasks = [s.pending_in(p, p.name) for p in s.projects]
        r = self.execute(s, proposal(completed_tasks=[completion(p, t) for p, t in zip(s.projects, tasks)]))
        self.assertTrue(all(t.status == 'completed' for t in tasks))
        self.assertEqual(len([x for x in s.rows if isinstance(x, TaskChange)]), 2)
        self.assertEqual(len(r['execution']['project_ids']), 2)

    def test_global_date_ambiguity_does_not_block_completion(self):
        s = V2Session(); t = s.pending('Enviar accesos')
        self.execute(s, proposal(completed_tasks=[completion(s.projects[0], t)], ambiguities=[{'type': 'date', 'message': 'Martes o miércoles'}]))
        self.assertEqual(t.status, 'completed')

    def test_one_ambiguous_completion_other_applied(self):
        s = V2Session(); a = s.pending('Accesos'); b = s.pending('Presentación'); c = s.pending('Otra presentación')
        r = self.execute(s, proposal(completed_tasks=[completion(s.projects[0], a), completion(s.projects[0], b, alternatives=[c.id])]))
        self.assertEqual([a.status, b.status, c.status], ['completed', 'open', 'open'])
        self.assertEqual([i['status'] for i in r['execution']['items']], ['completed', 'not_applied'])

    def test_project_ambiguity_only_blocks_own_item(self):
        s = V2Session(); tasks = [s.pending_in(p, p.name) for p in s.projects]
        self.execute(s, proposal(completed_tasks=[completion(s.projects[0], tasks[0]), completion(s.projects[1], tasks[1], ambiguities=[{'type': 'project', 'message': 'Cuál proyecto'}])]))
        self.assertEqual([t.status for t in tasks], ['completed', 'open'])

    def test_dirty_all_projects_with_distinct_origins(self):
        self.assert_multi('updates', {'update_text': 'Accesos enviados'}, ProjectUpdate)
        origins = [c.kwargs['origin_key'] for c in self.last_dirty.call_args_list]
        self.assertEqual(len(set(origins)), 2)

    def test_execution_preserves_proposal_and_each_item(self):
        s = V2Session(); t = s.pending('Accesos')
        p = proposal(completed_tasks=[completion(s.projects[0], t, confidence=.5)])
        self.execute(s, p)
        run = next(row for row in s.rows if isinstance(row, ProcessingRun))
        self.assertEqual(run.result['completed_tasks'], p.model_dump(mode='json')['completed_tasks'])
        self.assertEqual(run.result['execution']['items'][0]['reason'], 'low_confidence')

    def test_v1_still_readable_and_cached_without_changes(self):
        s = V2Session(); p = v1_plan(s.projects[0].id)
        run = ProcessingRun(id=uuid4(), source_id=s.source.id, result=p.model_dump(mode='json'), provider='anthropic', model='old', prompt_version='message-interpreter-v1')
        s.rows.append(run); s.source.latest_processing_run_id = run.id
        before = copy.deepcopy(vars(s.source)), copy.deepcopy(run.result)
        with patch('app.services.message_interpreter.interpret') as interpret:
            result = process_text_source(s, s.source.id, config())
        interpret.assert_not_called()
        self.assertEqual(result['status'], 'already_processed')
        self.assertEqual((vars(s.source), run.result), before)
        self.assertEqual(read_action_plan(run.result), p)

    def test_new_processing_does_not_mutate_historical_rows(self):
        s = V2Session(); old = s.pending('Antigua')
        before = copy.deepcopy({k: v for k, v in vars(old).items() if k != "_sa_instance_state"})
        historical = ProcessingRun(id=uuid4(), source_id=uuid4(), result=v1_plan().model_dump(mode='json'), provider='anthropic', model='old', prompt_version='old')
        s.rows.append(historical); result_before = copy.deepcopy(historical.result)
        self.execute(s, proposal(updates=[scope(s.projects[0], update_text='Accesos enviados', evidence='ya envié accesos')]))
        self.assertEqual({k: v for k, v in vars(old).items() if k != "_sa_instance_state"}, before)
        self.assertEqual(historical.result, result_before)

    def test_mixed_query_other_project_after_commit(self):
        from app.schemas.reasoning import QueryPlan
        s = V2Session(); t = s.pending('Accesos')
        query = {'question': 'Qué falta de Catu?', 'retrieval': QueryPlan(scope_type='project', scope_value='Catu', semantic_queries=[], recent_sources_limit=1, include_tasks=True, include_decisions=False, include_project_memory=False, time_basis='none').model_dump(mode='json')}
        p = ActionPlanV2(schema_version='action-plan-v2', interaction='mixed', summary='Resumen', completed_tasks=[completion(s.projects[0], t)], query=query)
        r = self.execute(s, p)
        def answer(*args, **kwargs):
            self.assertFalse(s.active)
            self.assertEqual(t.status, 'completed')
            self.assertEqual(kwargs['interpreted_plan'].scope_value, 'Catu')
            return 'Pendiente Catu'
        with patch('app.services.message_handling.answer_reasoning', side_effect=answer):
            text = respond_to_plan(s, r, config())
        self.assertIn('Pendiente Catu', text)

    def test_uuid_not_in_candidates_rejected(self):
        s = V2Session(); t = s.pending('Accesos')
        p = proposal(completed_tasks=[completion(s.projects[0], t, task_id=uuid4())])
        with self.assertRaises(ExtractionError): self.execute(s, p)
        self.assertEqual(t.status, 'open')

    def test_completion_different_project_rejected(self):
        s = V2Session(); t = s.pending('Accesos')
        with self.assertRaises(ExtractionError): self.execute(s, proposal(completed_tasks=[completion(s.projects[1], t)]))

    def test_owner_date_ambiguity_clears_only_uncertain_fields(self):
        s = V2Session()
        r = self.execute(s, proposal(tasks=[scope(s.projects[0], title='Revisar', description=None, owner_text='Ana', due_at=NOW, evidence='ya envié accesos', ambiguities=[{'type': 'owner', 'message': 'Ana o Luis'}, {'type': 'date', 'message': 'Día incierto'}])]))
        task = next(row for row in s.rows if isinstance(row, Task))
        self.assertIsNone(task.owner_text); self.assertIsNone(task.due_at)
        self.assertEqual(r['execution']['items'][0]['status'], 'applied')


    def test_audio_transcript_uses_v2(self):
        from app.services.processing import process_source
        s = V2Session()
        s.source.source_type = 'telegram_audio'
        p = proposal(updates=[scope(s.projects[0], update_text='Accesos enviados', evidence='ya envié accesos')])
        with patch('app.services.processing.transcribe_audio', return_value=s.source.id), patch(
                'app.services.processing.process_text_source', return_value={'schema_version': 'action-plan-v2'}) as process:
            process_source(s, s.source.id, config())
        process.assert_called_once()
        from pathlib import Path
        self.assertIn('audio.raw_metadata["processing_schema"]', Path('app/services/transcription.py').read_text(encoding='utf8'))

    def test_long_document_uses_v2_and_per_project_provenance(self):
        from app.schemas.extraction import Extraction
        from app.schemas.hierarchical import ConsolidatedActionPlanV2
        from app.models import TaskEvidence
        from test_hierarchical import output, QUOTE
        s = V2Session('SIMA Catu. ' + 'x' * 5880 + ' ' + QUOTE + ' ' + 'y' * 31000)
        calls = []
        def claude(settings, system, data, schema, client=None, **kwargs):
            calls.append(schema)
            if schema is Extraction:
                return Extraction.model_validate(output(data['project_id'], QUOTE in data['source_text']))
            self.assertIs(schema, ConsolidatedActionPlanV2)
            refs = [i['candidate_id'] for i in data['tasks']]
            return ConsolidatedActionPlanV2(schema_version='action-plan-v2', interaction='update', summary='Resumen',
                tasks=[scope(s.projects[1], title='Send report', description=None, owner_text='Ana', due_at=None, evidence=QUOTE, candidate_ids=refs)],
                decisions=[], people=['Ana'], dispositions=[{'candidate_id': ref, 'status': 'merged', 'final_index': 0} for ref in refs])
        with patch('app.services.hierarchical_llm.call_json', side_effect=claude), patch('app.services.action_execution_v2.mark_dirty'):
            result = process_text_source(s, s.source.id, config())
        self.assertEqual(result['schema_version'], 'action-plan-v2')
        self.assertEqual(calls.count(ConsolidatedActionPlanV2), 1)
        task = next(r for r in s.rows if isinstance(r, Task))
        self.assertEqual(task.project_id, s.projects[1].id)
        locations = [r for r in s.rows if isinstance(r, TaskEvidence)]
        self.assertTrue(locations)
        self.assertTrue(all(s.source.raw_content[r.char_start:r.char_end] == r.evidence for r in locations))
        run = next(r for r in s.rows if isinstance(r, ProcessingRun))
        self.assertEqual(run.prompt_version, 'action-plan-consolidation-v2')


    def test_project_memory_source_view_excludes_other_project(self):
        from app.services.project_source_view import project_source_view
        s, r = self.assert_multi('updates', {'update_text': 'Accesos enviados'}, ProjectUpdate)
        run = next(row for row in s.rows if isinstance(row, ProcessingRun))
        run.result['updates'][1]['evidence'] = 'terminé presentación'
        run.result['execution']['items'][1]['title'] = 'Presentación de Catu'
        summary, excerpt = project_source_view(s.source, run, s.projects[0].id)
        self.assertNotIn('Catu', summary)
        self.assertNotIn('presentación', excerpt)
        self.assertEqual(read_action_plan(run.result).schema_version, 'action-plan-v2')

    def test_unconstrained_provider_json_still_strictly_validated(self):
        from unittest.mock import MagicMock
        import json
        from app.services.reasoning_llm import call_json, ReasoningError
        client = MagicMock()
        p = proposal()
        client.post.return_value.json.return_value = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '```json\n' + p.model_dump_json() + '\n```'}]}
        self.assertEqual(call_json(config(), 'rules', {}, ActionPlanV2, client, constrained=False), p)
        self.assertNotIn('output_config', client.post.call_args.kwargs['json'])
        client.post.return_value.json.return_value['content'][0]['text'] = p.model_dump_json()[:-1] + ', "sql": "invalid"}'
        with self.assertRaises(ReasoningError): call_json(config(), 'rules', {}, ActionPlanV2, client, constrained=False)


    def test_candidate_race_blocks_only_affected_project(self):
        from app.services.action_execution import execute_plan
        s = V2Session(); tasks = [s.pending_in(p, p.name) for p in s.projects]
        with s.begin():
            context = assemble_v2_context(s, s.source, s.projects, config())
            s.pending_in(s.projects[0], 'Nuevo pendiente concurrente')
            p = proposal(completed_tasks=[completion(project, task) for project, task in zip(s.projects, tasks)])
            with patch('app.services.action_execution_v2.mark_dirty'), patch('app.services.task_management.mark_dirty'):
                run = execute_plan(s, s.source, p, context, config(), 'message-interpreter-v2')
        self.assertEqual([t.status for t in tasks], ['open', 'completed'])
        self.assertEqual(run.result['execution']['items'][0]['reason'], 'candidates_changed')

    def test_task_snapshot_race_blocks_only_changed_task(self):
        from app.services.action_execution import execute_plan
        s = V2Session(); first=s.pending('Accesos'); second=s.pending('Presentación')
        with s.begin():
            context = assemble_v2_context(s, s.source, s.projects, config())
            first.owner_text = 'Nuevo responsable'
            p = proposal(completed_tasks=[completion(s.projects[0], task) for task in (first, second)])
            with patch('app.services.action_execution_v2.mark_dirty'), patch('app.services.task_management.mark_dirty'):
                run = execute_plan(s, s.source, p, context, config(), 'message-interpreter-v2')
        self.assertEqual([first.status, second.status], ['open', 'completed'])
        self.assertEqual(run.result['execution']['items'][0]['reason'], 'task_changed')

    def test_query_scope_ambiguity_preserves_independent_completion(self):
        from app.schemas.reasoning import QueryPlan
        s=V2Session(); task=s.pending('Accesos')
        p=ActionPlanV2(schema_version='action-plan-v2', interaction='mixed', summary='Resumen',
            completed_tasks=[completion(s.projects[0], task)], query={'question': 'Qué falta?', 'retrieval': QueryPlan().model_dump(),
            'ambiguities': [{'type': 'query_scope', 'message': 'Catu o SIMA'}]})
        result=self.execute(s,p)
        with patch('app.services.message_handling.answer_reasoning') as answer:
            reply=respond_to_plan(s,result,config())
        answer.assert_not_called()
        self.assertEqual(task.status,'completed')
        self.assertIn('Catu o SIMA',reply)

    def test_pure_v2_queries_excluded_and_multi_scope_sql_compiles(self):
        from app.services.queries import current_source, source_statement, Scope
        from app.models import Source
        from sqlalchemy import select
        query=select(Source.id).where(current_source()).compile(dialect=postgresql.dialect())
        self.assertIn('action-plan-v2',str(query.params))
        s=V2Session()
        scoped=source_statement(Scope([p.id for p in s.projects], 'two')).compile(dialect=postgresql.dialect())
        self.assertIn('membership_run',str(scoped))
        self.assertIn('project_ids',str(scoped.params))
        empty=source_statement(Scope([], 'empty')).compile(dialect=postgresql.dialect())
        self.assertIn('false',str(empty))
