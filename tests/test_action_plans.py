"""Future canonical route. LLM fixtures exercise validation, not model accuracy."""
import copy
import io
import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace as Row
from unittest.mock import MagicMock, patch
from uuid import uuid4
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from app.models import Task, TaskChange, ProcessingRun, ProjectUpdate, UpdateEvidence, Source
from app.schemas.action_plan import ActionPlan
from app.schemas.extraction import Extraction
from app.schemas.hierarchical import ConsolidatedActionPlan
from app.schemas.reasoning import QueryPlan
from app.services.claude import ExtractionError
from app.services.processing import process_text_source, run_result
from app.services.action_context import assemble_context
from app.services.message_interpreter import interpret, validate_plan
from app.services.message_handling import handle_message, respond_to_plan
from app.services.hierarchical_llm import validate_partial, consolidation_input, validate_consolidation
from app.services.reasoning import retrieve, trace_context
from app.services.queries import Scope, source_statement
from app.services.retrieval_budget import bound_context
from test_hierarchical import FakeSession, config, output

NOW = datetime(2026, 10, 6, 15, tzinfo=timezone.utc)


def plan(project_id=None, **kwargs):
    return ActionPlan.model_validate(output(project_id) | {
        'schema_version': 'action-plan-v1', 'interaction': 'update', 'scope_confidence': .99,
        'updates': [], 'completed_tasks': [], 'query': None, 'ambiguities': []} | kwargs)


def new_task(title, evidence):
    return {'title': title, 'description': None, 'owner_text': None, 'due_at': None, 'evidence': evidence}


def completion(task, quote, **kwargs):
    return {'task_id': str(task.id), 'confidence': .99, 'evidence': quote,
            'state': 'performed', 'alternatives': []} | kwargs


class ActionSession(FakeSession):
    def __init__(self, text):
        super().__init__(text)
        self.source.raw_metadata['processing_schema'] = 'action-plan-v1'
        for project in self.projects:
            project.area = project.company = None
        self.locked = []

    @contextmanager
    def begin(self):
        before = [(row, copy.deepcopy(vars(row))) for row in self.rows]
        try:
            with super().begin():
                yield
        except BaseException:
            for row, values in before:
                row.__dict__.clear()
                row.__dict__.update(values)
            raise

    def scalars(self, statement):
        model = statement.column_descriptions[0]['entity']
        params = statement.compile(dialect=postgresql.dialect()).params
        if model is ProjectUpdate:
            return Row(all=lambda: [])
        if model is Task:
            tasks = [t for t in self.rows if isinstance(t, Task)]
            if statement.column_descriptions[0]['name'] == 'source_id':
                return Row(all=lambda: [t.source_id for t in tasks if t.id in params['id_1']])
            tasks = [t for t in tasks if t.status in ('open', 'in_progress')]
            if statement.column_descriptions[0]['name'] == 'id':
                return Row(all=lambda: [t.id for t in tasks])
            if 'id_1' in params:
                tasks = [t for t in tasks if t.id in params['id_1']]
            if statement._for_update_arg is not None:
                self.locked.extend(t.id for t in tasks)
            limit = statement._limit_clause.value if statement._limit_clause is not None else 1000
            return Row(all=lambda: tasks[:limit])
        return super().scalars(statement)

    def get(self, klass, identifier):
        if klass is Source:
            return self.source
        if klass.__name__ == 'Project':
            return next((p for p in self.projects if p.id == identifier), None)
        return super().get(klass, identifier)

    def scalar(self, statement):
        if statement.column_descriptions[0]['entity'].__name__ == 'Project':
            params = statement.compile(dialect=postgresql.dialect()).params
            return next((p.id for p in self.projects if p.id == params['id_1']), None)
        return super().scalar(statement)

    def pending(self, title):
        item = Task(id=uuid4(), project_id=self.projects[0].id, title=title, status='open',
                    source_id=None, processing_run_id=None, created_at=NOW, updated_at=NOW)
        self.rows.append(item)
        return item


class ActionPlanTests(unittest.TestCase):
    def execute(self, session, result):
        with patch('app.services.message_interpreter.interpret', return_value=result) as call, patch(
            'app.services.processing.extract') as old_extract, patch('app.services.task_completion.propose_completion') as old_completion:
            response = process_text_source(session, session.source.id, config())
            again = process_text_source(session, session.source.id, config())
        self.assertEqual(call.call_count, 1)
        self.assertEqual(again['run_id'], response['run_id'])
        old_extract.assert_not_called()
        old_completion.assert_not_called()
        self.assertFalse(session.active)
        return response

    def test_dashboard_mixed_categories_and_original_preserved(self):
        text = 'Ya tuvimos la reunión con Cobranzas. Los datos cuadraron y las líneas recomendadas les hicieron sentido. Mañana tengo que mandar los accesos.'
        session = ActionSession(text)
        task = session.pending('Presentar dashboard a Créditos y Cobranzas')
        originals = copy.deepcopy((session.source.raw_content, session.source.raw_metadata))
        result = plan(task.project_id, updates=[
            {'update_text': 'Se realizó la reunión con Cobranzas.', 'evidence': 'Ya tuvimos la reunión con Cobranzas.'},
            {'update_text': 'Cobranzas validó los datos.', 'evidence': 'Los datos cuadraron'},
            {'update_text': 'Las líneas recomendadas hicieron sentido a Cobranzas.', 'evidence': 'las líneas recomendadas les hicieron sentido.'}],
            completed_tasks=[completion(task, 'Ya tuvimos la reunión con Cobranzas.')],
            tasks=[new_task('Enviar accesos', 'Mañana tengo que mandar los accesos.')])
        response = self.execute(session, result)
        self.assertEqual(response['updates_count'], 3)
        self.assertEqual(response['decisions_count'], 0)
        self.assertEqual(task.status, 'completed')
        self.assertEqual(len([r for r in session.rows if isinstance(r, ProjectUpdate)]), 3)
        self.assertEqual((session.source.raw_content, session.source.raw_metadata), originals)
        self.assertIn(task.id, session.locked)
        run = next(r for r in session.rows if isinstance(r, ProcessingRun))
        self.assertEqual(run.result['schema_version'], 'action-plan-v1')
        self.assertEqual(run.prompt_version, 'message-interpreter-v1')

    def test_catu_requirement_decision_and_new_task(self):
        text = 'Para Catu quiero cambiar la validación. Si el ID no parece un número debe pedir DNI y nombre para saber si es vendedor o supervisor. Esto hay que implementarlo.'
        session = ActionSession(text)
        result = plan(session.projects[0].id,
            updates=[{'update_text': 'Catu recibió un requerimiento de validación de ID.', 'evidence': 'Para Catu quiero cambiar la validación.'}],
            decisions=[{'decision_text': 'Ante ID no numérico, pedir DNI y nombre para distinguir vendedor o supervisor.',
                        'decided_at': None, 'evidence': 'Si el ID no parece un número debe pedir DNI y nombre para saber si es vendedor o supervisor.'}],
            tasks=[new_task('Implementar flujo de validación de ID', 'Esto hay que implementarlo.')])
        response = self.execute(session, result)
        self.assertEqual((response['updates_count'], response['tasks_count'], response['decisions_count']), (1, 1, 1))

    def test_it_sending_does_not_complete_consolidation(self):
        text = 'Ya envié los correos de validación a los responsables de IT.'
        session = ActionSession(text)
        send, consolidate = session.pending('Enviar correos de validación'), session.pending('Consolidar respuestas')
        self.execute(session, plan(send.project_id, completed_tasks=[completion(send, text)],
            updates=[{'update_text': 'Correos de validación enviados a IT.', 'evidence': text}]))
        self.assertEqual(send.status, 'completed')
        self.assertEqual(consolidate.status, 'open')

    def test_two_completions_same_source_have_distinct_audit_and_memory_events(self):
        text = 'Ya envié los correos y terminé la presentación.'
        session = ActionSession(text)
        tasks = [session.pending('Enviar correos'), session.pending('Terminar presentación')]
        with patch('app.services.task_management.mark_dirty') as dirty:
            self.execute(session, plan(tasks[0].project_id, completed_tasks=[completion(t, text) for t in tasks]))
        changes = [r for r in session.rows if isinstance(r, TaskChange)]
        self.assertEqual(len(changes), 2)
        self.assertEqual({c.command_source_id for c in changes}, {session.source.id})
        self.assertEqual({c.task_id for c in changes}, {t.id for t in tasks})
        self.assertEqual({c.action for c in changes}, {'natural_completion'})
        self.assertTrue(all(c.before['status'] == 'open' and c.after['status'] == 'completed' for c in changes))
        self.assertEqual(dirty.call_count, 2)
        self.assertEqual(len({c.kwargs['origin_key'] for c in dirty.call_args_list}), 2)

    def test_nonperformed_and_low_confidence_never_complete(self):
        for state, text in [('future', 'Mañana enviaré los correos.'), ('negated', 'No pude enviar los correos.'),
                            ('pending', 'Sigue pendiente enviar los correos.'), ('mentioned', 'Hablamos de enviar los correos.')]:
            with self.subTest(state=state):
                session = ActionSession(text)
                task = session.pending('Enviar correos')
                self.execute(session, plan(task.project_id, completed_tasks=[completion(task, text, state=state)]))
                self.assertEqual(task.status, 'open')
                self.assertFalse(any(isinstance(r, TaskChange) for r in session.rows))
        session = ActionSession('Ya envié correos.')
        task = session.pending('Enviar correos')
        self.execute(session, plan(task.project_id, completed_tasks=[completion(task, session.source.raw_content, confidence=.94)]))
        self.assertEqual(task.status, 'open')

    def test_decision_vs_proposal_fixtures(self):
        for text, decision in [('Evaluamos ambas opciones y decidimos usar Databricks.', True),
                               ('Tal vez deberíamos usar Databricks.', False)]:
            session = ActionSession(text)
            result = plan(session.projects[0].id, decisions=[{'decision_text': 'Usar Databricks', 'decided_at': None,
                         'evidence': 'decidimos usar Databricks.'}] if decision else [],
                         updates=[] if decision else [{'update_text': 'Databricks fue propuesto como opción.', 'evidence': text}])
            response = self.execute(session, result)
            self.assertEqual(response['decisions_count'], int(decision))
            self.assertEqual(response['tasks_count'], 0)

    def test_semantic_scope_does_not_require_exact_matching_and_ambiguity_keeps_source(self):
        session = ActionSession('McKinsey FrontRunner: revisar la factura.')
        front = session.projects[0]
        front.name, front.slug = 'FrontRunner', 'frontrunner'
        session.projects.append(Row(id=uuid4(), name='Operating Model / McKinsey', slug='operating-model',
                                    aliases=[Row(alias='McKinsey')], area=None, company=None))
        with patch('app.services.processing.match_source_project') as old_match:
            self.execute(session, plan(front.id, tasks=[new_task('Revisar factura', 'revisar la factura.')]))
        self.assertEqual(session.source.primary_project_id, front.id)
        old_match.assert_not_called()
        session = ActionSession('Hablé con McKinsey. Hay cambios.')
        response = self.execute(session, plan(None, scope_confidence=.4, ambiguities=['¿A cuál proyecto de McKinsey te refieres?'],
            updates=[{'update_text': 'Se conversó con McKinsey sobre cambios.', 'evidence': session.source.raw_content}]))
        self.assertIsNone(session.source.primary_project_id)
        self.assertTrue(response['ambiguities'])

    def test_ids_and_literal_evidence_are_validated_before_any_mutation(self):
        for change in ('project', 'task', 'evidence'):
            session = ActionSession('Ya envié correos.')
            task = session.pending('Enviar correos')
            payload = plan(task.project_id, completed_tasks=[completion(task, session.source.raw_content)]).model_dump(mode='json')
            if change == 'project': payload['project_id'] = str(uuid4())
            if change == 'task': payload['completed_tasks'][0]['task_id'] = str(uuid4())
            if change == 'evidence': payload['completed_tasks'][0]['evidence'] = 'inventado'
            with patch('app.services.message_interpreter.interpret', return_value=ActionPlan.model_validate(payload)), self.assertRaises(ExtractionError):
                process_text_source(session, session.source.id, config())
            self.assertEqual(task.status, 'open')
            self.assertEqual(session.source.processing_status, 'failed')
            self.assertFalse(any(isinstance(r, ProcessingRun) for r in session.rows))

    def test_material_alternatives_and_concurrent_change_cancel_completions(self):
        session = ActionSession('Ya hice eso.')
        first, second = session.pending('Enviar informe'), session.pending('Enviar reporte')
        self.execute(session, plan(first.project_id, completed_tasks=[completion(first, session.source.raw_content, alternatives=[second.id])]))
        self.assertEqual([first.status, second.status], ['open', 'open'])
        session = ActionSession('Ya envié informe.')
        task = session.pending('Enviar informe')
        def changed(settings, context):
            task.owner_text = 'Otro responsable'
            return plan(task.project_id, completed_tasks=[completion(task, session.source.raw_content)])
        with patch('app.services.message_interpreter.interpret', side_effect=changed):
            result = process_text_source(session, session.source.id, config())
        self.assertEqual(task.status, 'open')
        self.assertTrue(result['ambiguities'])

    def test_transaction_rolls_back_tasks_and_updates_if_audit_fails(self):
        session = ActionSession('Ya envié correos y mañana reviso contrato.')
        task = session.pending('Enviar correos')
        result = plan(task.project_id, completed_tasks=[completion(task, 'Ya envié correos')],
            updates=[{'update_text': 'Correos enviados.', 'evidence': 'Ya envié correos'}],
            tasks=[new_task('Revisar contrato', 'mañana reviso contrato.')])
        with patch('app.services.message_interpreter.interpret', return_value=result), patch(
            'app.services.action_execution.record_task_change', side_effect=RuntimeError('DB failure')), self.assertRaises(RuntimeError):
            process_text_source(session, session.source.id, config())
        self.assertEqual(task.status, 'open')
        self.assertEqual(len(session.rows), 1)
        self.assertIsNone(session.source.latest_processing_run_id)

    def test_new_competing_candidate_during_interpretation_blocks_completion(self):
        session = ActionSession('Ya envié informe.')
        task = session.pending('Enviar informe')
        def changed(settings, context):
            session.pending('Enviar informe')
            return plan(task.project_id, completed_tasks=[completion(task, session.source.raw_content)])
        with patch('app.services.message_interpreter.interpret', side_effect=changed):
            response = process_text_source(session, session.source.id, config())
        self.assertEqual(task.status, 'open')
        self.assertTrue(response['ambiguities'])
        self.assertFalse(any(isinstance(r, TaskChange) for r in session.rows))

    def test_schema_consistency_strict_confidence_and_single_call(self):
        for changes in ({'interaction': 'query'}, {'interaction': 'mixed'}, {'scope_confidence': True},
                        {'scope_confidence': '0.99'}, {'scope_confidence': float('nan')}, {'sql': 'SELECT 1'}):
            with self.assertRaises(ValidationError):
                ActionPlan.model_validate(plan().model_dump() | changes)
        with patch('app.services.message_interpreter.call_json', return_value=plan()) as call:
            interpret(config(), {'message': 'mensaje'})
        call.assert_called_once()
        self.assertIs(call.call_args.args[3], ActionPlan)

    def test_context_is_bounded_and_truncated_candidates_disable_completions(self):
        session = ActionSession('Ya envié el informe.')
        for i in range(105): session.pending('Enviar informe ' + str(i))
        with session.begin():
            context = assemble_context(session, session.source, session.projects, config())
        self.assertFalse(context['tasks_complete'])
        self.assertLessEqual(len(context['open_tasks']), 100)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), config().reasoning_context_max_chars)

    def test_mixed_question_answers_after_commit_with_no_second_interpretation(self):
        session = ActionSession('Ya envié los correos de IT. ¿Qué me queda pendiente de McKinsey?')
        task = session.pending('Enviar correos IT')
        result = plan(task.project_id, interaction='mixed', completed_tasks=[completion(task, 'Ya envié los correos de IT.')],
            updates=[{'update_text': 'Correos enviados a IT.', 'evidence': 'Ya envié los correos de IT.'}],
            query={'question': '¿Qué me queda pendiente de McKinsey?', 'retrieval': {'scope_type': 'project', 'scope_value': 'SIMA'}})
        saved = {'status': 'saved', 'source_id': str(session.source.id)}
        def answer(*args, **kwargs):
            self.assertFalse(session.active)
            self.assertEqual(task.status, 'completed')
            self.assertIsNotNone(session.source.latest_processing_run_id)
            self.assertIsInstance(kwargs['interpreted_plan'], QueryPlan)
            return 'Queda consolidar respuestas.'
        with patch('app.services.message_handling.ingest_update', return_value=saved), patch(
            'app.services.message_handling.process_source', side_effect=lambda *a: process_text_source(session, session.source.id, config())), patch(
            'app.services.message_interpreter.interpret', return_value=result), patch(
            'app.services.message_handling.answer_reasoning', side_effect=answer), patch('app.services.message_handling.maybe_index'):
            reply = handle_message(session, {}, 123, config())
        self.assertIn('Completada: Enviar correos IT', reply['answer'])
        self.assertIn('Queda consolidar respuestas.', reply['answer'])

    def test_query_does_not_create_mutations_or_index_and_excluded_from_evidence(self):
        result = {'source_id': str(uuid4()), 'action_plan': True, 'interaction': 'query', 'updates_count': 0,
                  'tasks_count': 0, 'decisions_count': 0, 'query': {'question': '¿Cómo está SIMA?', 'retrieval': {}}}
        with patch('app.services.message_handling.ingest_update', return_value={'status': 'saved', 'source_id': result['source_id']}), patch(
            'app.services.message_handling.process_source', return_value=result), patch(
            'app.services.message_handling.answer_reasoning', return_value='Respuesta'), patch('app.services.message_handling.maybe_index') as index:
            handle_message(MagicMock(), {}, 123, config())
        index.assert_not_called()
        sql = str(source_statement(Scope(None, 'global')).compile(dialect=postgresql.dialect()))
        self.assertIn('NOT (EXISTS', sql)
        self.assertIn('processing_runs.result', sql)

    def test_legacy_replay_does_not_reprocess_or_change_metadata(self):
        session = ActionSession('Old message')
        session.source.raw_metadata = {'legacy': True}
        with patch('app.services.message_handling.ingest_update', return_value={'status': 'duplicate', 'source_id': str(session.source.id)}), patch(
            'app.services.message_handling.process_source') as process:
            reply = handle_message(session, {}, 123, config())
        process.assert_not_called()
        self.assertIn('histórico', reply['answer'])
        self.assertEqual(session.source.raw_metadata, {'legacy': True})
        old = Row(id=uuid4(), source_id=uuid4(), result=output())
        self.assertNotIn('action_plan', run_result(old))

    def test_explicit_legacy_reprocess_can_opt_into_new_plan_without_metadata_rewrite(self):
        session = ActionSession('Los datos cuadraron.')
        session.source.raw_metadata = {'legacy': True}
        old = ProcessingRun(id=uuid4(), source_id=session.source.id, provider='anthropic', model='old',
                            prompt_version='old-prompt', result=output())
        session.rows.append(old)
        session.source.latest_processing_run_id = old.id
        original_result = copy.deepcopy(old.result)
        proposal = plan(session.projects[0].id, updates=[{'update_text': 'Datos validados.', 'evidence': session.source.raw_content}])
        with patch('app.services.message_interpreter.interpret', return_value=proposal), patch('app.services.processing.extract') as legacy:
            response = process_text_source(session, session.source.id, config(), force=True, action_plan=True)
        self.assertTrue(response['action_plan'])
        self.assertEqual(session.source.raw_metadata, {'legacy': True})
        self.assertEqual(old.result, original_result)
        legacy.assert_not_called()
        from app.services.processing import SourceNotProcessable
        with self.assertRaises(SourceNotProcessable):
            process_text_source(session, session.source.id, config(), action_plan=True)

    def test_update_marks_dirty_and_preserves_event_origin(self):
        session = ActionSession('Los datos cuadraron.')
        with patch('app.services.action_execution.mark_dirty') as dirty:
            self.execute(session, plan(session.projects[0].id, updates=[{
                'update_text': 'Los datos fueron validados.', 'evidence': session.source.raw_content}]))
        dirty.assert_called_once()
        self.assertEqual(dirty.call_args.kwargs['event_type'], 'project_update')
        self.assertEqual(dirty.call_args.kwargs['source_id'], session.source.id)

    def test_hierarchy_updates_keep_literal_offsets_candidate_provenance(self):
        session = ActionSession('Los datos cuadraron.')
        source = session.source
        chunk = Row(id=uuid4(), content=source.raw_content, char_start=0, char_end=len(source.raw_content))
        extraction = Extraction.model_validate(output() | {'updates': [{'update_text': 'Datos validados.', 'evidence': source.raw_content}]})
        partial = validate_partial(extraction, source, chunk)
        part = Row(id=uuid4(), source_chunk_id=chunk.id, part_index=0, result=partial, generation_id=uuid4(), base_part_id=uuid4())
        data, candidates = consolidation_input(source, [part], session.projects, config())
        ref = data['updates'][0]['candidate_id']
        final = ConsolidatedActionPlan.model_validate(plan(session.projects[0].id).model_dump(mode='json') | {
            'updates': [{'update_text': 'Datos validados.', 'evidence': source.raw_content, 'candidate_ids': [ref]}],
            'dispositions': [{'candidate_id': ref, 'status': 'kept', 'final_index': 0}]})
        result, provenance = validate_consolidation(final, candidates, source, session.projects)
        self.assertIsInstance(result, ActionPlan)
        loc = provenance['updates'][0][0]
        self.assertEqual(loc['generation_part_id'], str(part.id))
        self.assertEqual(source.raw_content[loc['char_start']:loc['char_end']], loc['evidence'])
        with session.begin():
            context = assemble_context(session, source, session.projects, config(), include_message=False)
            from app.services.action_execution import execute_plan
            execute_plan(session, source, result, context, config(), 'action-plan-consolidation-v1', provenance=provenance)
        self.assertTrue(any(isinstance(r, UpdateEvidence) and r.source_chunk_id == chunk.id for r in session.rows))

    def test_full_long_source_uses_new_parts_and_one_final_plan_then_reuses_it(self):
        quote = 'Los datos cuadraron.'
        session = ActionSession('x' * 5880 + ' ' + quote + ' ' + 'y' * 31000)
        calls = []
        def claude(settings, system, data, schema, client=None, **kwargs):
            calls.append(schema)
            if schema is Extraction:
                self.assertIn('solo updates', system)
                return Extraction.model_validate(output() | {'updates': [
                    {'update_text': 'Datos validados.', 'evidence': quote}] if quote in data['source_text'] else []})
            self.assertIs(schema, ConsolidatedActionPlan)
            refs = [u['candidate_id'] for u in data['updates']]
            self.assertIsNone(data['interpretation_context']['message'])
            return ConsolidatedActionPlan.model_validate(plan(session.projects[0].id).model_dump(mode='json') | {
                'updates': [{'update_text': 'Datos validados.', 'evidence': quote, 'candidate_ids': refs}],
                'dispositions': [{'candidate_id': r, 'status': 'merged', 'final_index': 0} for r in refs]})
        with patch('app.services.hierarchical_llm.call_json', side_effect=claude), patch(
            'app.services.processing.extract') as old:
            first = process_text_source(session, session.source.id, config())
            second = process_text_source(session, session.source.id, config())
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertEqual(first['updates_count'], 1)
        self.assertEqual(calls.count(ConsolidatedActionPlan), 1)
        self.assertEqual(calls.count(Extraction), len(session.parts))
        self.assertEqual({p.prompt_version for p in session.parts.values()}, {'action-plan-part-v1'})
        run = next(r for r in session.rows if isinstance(r, ProcessingRun))
        self.assertEqual(run.prompt_version, 'action-plan-consolidation-v1')
        self.assertEqual(run.result['schema_version'], 'action-plan-v1')
        self.assertEqual(len([r for r in session.rows if isinstance(r, ProjectUpdate)]), 1)
        self.assertGreaterEqual(len([r for r in session.rows if isinstance(r, UpdateEvidence)]), 1)
        old.assert_not_called()

    def test_updates_retrieval_keeps_original_and_budget_trace(self):
        source = Row(id=uuid4(), raw_content='Cita literal original.')
        update = Row(id=uuid4(), processing_run_id=uuid4(), project_id=uuid4(), update_text='Hecho derivado.', event_at=None)
        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        session.execute.return_value.all.return_value = [(update, source)]
        query = QueryPlan(include_project_memory=False, include_tasks=False, include_decisions=False, include_recent_sources=False)
        with patch('app.services.reasoning.plan_scope', return_value=Scope(None, 'Global')):
            context = retrieve(session, query, config())
        self.assertTrue(context['updates'][0]['derived'])
        self.assertEqual(context['updates'][0]['original_excerpt']['text'], source.raw_content)
        trace = trace_context(context)
        self.assertNotIn('Cita literal original.', json.dumps(trace))
        self.assertIn('original_excerpt_sha256', trace['updates'][0])
        context['updates'] *= 100
        limited = bound_context(context, query, {'updates': 20}, 3000)
        self.assertLessEqual(len(json.dumps(limited, ensure_ascii=False)), 3000)
        self.assertTrue(limited['retrieval']['budget_reduced'])

    def test_new_migration_only_adds_tables_and_replaces_unique_without_backfill(self):
        stream = io.StringIO()
        with patch('app.config.get_settings', return_value=config(DATABASE_URL='postgresql://offline/test')):
            command.upgrade(Config('alembic.ini', output_buffer=stream), '0011_task_completion_attempts:0012_action_plans', sql=True)
        sql = stream.getvalue()
        for value in ('CREATE TABLE project_updates', 'CREATE TABLE update_evidence',
                      'DROP CONSTRAINT uq_task_changes_command_source_id', 'UNIQUE (command_source_id, task_id)',
                      'REFERENCES extraction_generation_parts', 'preserve_project_updates'):
            self.assertIn(value, sql)
        for value in ('DROP TABLE', 'INSERT INTO project_updates', 'UPDATE sources', 'UPDATE tasks', 'DELETE FROM', 'TRUNCATE'):
            self.assertNotIn(value, sql)
        import importlib
        with self.assertRaises(RuntimeError):
            importlib.import_module('migrations.versions.0012_action_plans').downgrade()


if __name__ == '__main__':
    unittest.main()
