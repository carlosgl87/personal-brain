import copy
import io
import json
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace as Row
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import httpx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.config import Settings, get_settings
from app.database import get_session
from app.main import app
from app.models import (Area, Company, Project, Source, Task, TaskChange,
                        TaskCompletionAttempt, ProjectMemoryEvent, ProjectMemoryState)
from app.schemas.extraction import Extraction
from app.services.intent_router import deterministic_intent
from app.services.processing import process_text_source
from app.services.project_memory_retrieval import retrieve_project_memories
from app.services.project_memory import snapshot_data, publish_memory
from app.services.queries import Scope
from app.services.task_completion import (CompletionProposal, candidate_statement, complete_from_note,
    completion_scope, has_completion_signal, propose_completion, validate_proposal)
from app.telegram import process_update


TEXT = 'Ya presenté el dashboard de cobranzas al equipo de créditos de Catusita.'
TITLE = 'Presentar la nueva pestaña al equipo de Créditos y Cobranzas.'
NOW = datetime.now(timezone.utc)


def settings(**values):
    with patch.dict('os.environ', {}, clear=True):
        return Settings(_env_file=None, **({'PROJECT_MEMORY_ENABLED': False,
            'LLM_API_KEY': 'fake', 'LLM_MODEL': 'test-model', 'TELEGRAM_BOT_TOKEN': 'fake',
            'TELEGRAM_USER_ID': '123'} | values))


def proposal(task_id, text=TEXT, **values):
    data = {'completion_detected': True, 'matched_task_id': str(task_id), 'confidence': .99,
            'reason_code': 'completed', 'evidence': text, 'alternatives': []} | values
    return CompletionProposal.model_validate_json(json.dumps(data))


class CompletionRepo:
    """In-memory transaction fixture; SQL eligibility is verified separately."""
    def __init__(self, text=TEXT):
        self.area = Row(id=uuid4(), name='Consultora', slug='consultora')
        self.company = Row(id=uuid4(), name='Catusita', slug='catusita')
        self.project = Row(id=uuid4(), name='Dashboard Cobranzas', slug='dashboard-cobranzas',
            area_id=self.area.id, company_id=self.company.id, area=self.area, company=self.company,
            aliases=[Row(alias='Dashboard de Cobranzas')])
        self.projects = [self.project]
        self.source = Row(id=uuid4(), source_type='telegram_text', external_source='telegram',
            raw_content=text, raw_metadata={'message': {'text': text, 'date': 1700000000}},
            primary_project_id=self.project.id, latest_processing_run_id=None,
            received_at=NOW, processed_at=None, processing_status='pending')
        self.task = Row(id=uuid4(), source_id=uuid4(), project_id=self.project.id,
            title=TITLE, description=None, owner_text=None, status='open', completed_at=None, due_at=None)
        self.tasks, self.attempts, self.changes, self.events = [self.task], [], [], []
        self.state = Row(project_id=self.project.id, current_version_id=None, is_dirty=False,
            change_revision=0, dirty_since=None, refresh_after=None, retry_after=None,
            last_change_at=NOW, reconciled_revision=0)
        self.selected_missing = False
        self.statements = []

    def begin(self):
        return nullcontext()

    def scalar(self, statement):
        self.statements.append(statement)
        entity = statement.column_descriptions[0]['entity']
        if entity is Source:
            return self.source
        if entity is TaskCompletionAttempt:
            return next(iter(self.attempts), None)
        if entity is TaskChange:
            return next(iter(self.changes), None)
        if entity is Task:
            return None if self.selected_missing or self.task.status == 'completed' else self.task
        if entity is ProjectMemoryState:
            return self.state
        if entity is ProjectMemoryEvent:
            return None
        raise AssertionError(entity)

    def scalars(self, statement):
        self.statements.append(statement)
        entity = statement.column_descriptions[0]['entity']
        rows = {Project: self.projects, Company: [self.company], Area: [self.area],
                Task: [t for t in self.tasks if t.status in ['open', 'in_progress'] and t.completed_at is None]}.get(entity)
        if rows is None:
            raise AssertionError(entity)
        return Row(all=lambda: rows)

    def execute(self, statement):
        self.statements.append(statement)

    def add(self, item):
        if isinstance(item, TaskChange):
            self.changes.append(item)
        elif isinstance(item, TaskCompletionAttempt):
            self.attempts.append(item)
        elif isinstance(item, ProjectMemoryEvent):
            self.events.append(item)


class TaskCompletionTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.clear()

    def complete(self, repo, decision=None, cfg=None):
        with patch('app.services.task_completion.propose_completion', return_value=decision or
                   proposal(repo.task.id, repo.source.raw_content.split('. ')[0])) as llm:
            answer = complete_from_note(repo, repo.source.id, cfg or settings())
        return answer, llm

    def test_completed_task_has_source_audit_and_original_note(self):
        repo = CompletionRepo()
        original = copy.deepcopy(vars(repo.source))
        answer, llm = self.complete(repo)
        self.assertEqual(repo.task.status, 'completed')
        self.assertEqual(vars(repo.source), original)
        self.assertIn('✅ Tarea completada: ' + TITLE, answer)
        change = repo.changes[0]
        self.assertEqual(change.action, 'natural_completion')
        self.assertEqual(change.command_source_id, repo.source.id)
        self.assertEqual(change.before['status'], 'open')
        self.assertEqual(change.after['status'], 'completed')
        self.assertIsNotNone(repo.task.completed_at.tzinfo)
        self.assertEqual(repo.attempts[0].result['outcome'], 'completed')
        self.assertEqual(llm.call_args.args[1][0]['title'], TITLE)

    def test_paraphrases_are_sent_for_semantic_resolution_without_exact_title(self):
        for text in ['Ya tuve la reunión para mostrarle la nueva pestaña al equipo de créditos.',
                     'Ya hice la presentación del dashboard de cobranzas.',
                     'La presentación con el equipo de créditos ya quedó hecha.',
                     'Ya terminé la presentación de la nueva pestaña de cobranzas.',
                     'Ya se presentó la nueva pestaña al equipo.',
                     'Hoy presenté el dashboard de cobranzas y al equipo le gustó la nueva vista.']:
            with self.subTest(text=text):
                repo = CompletionRepo(text)
                _, llm = self.complete(repo)
                llm.assert_called_once()
                self.assertEqual(repo.task.status, 'completed')

    def test_future_pending_negation_and_preparation_skip_provider_and_changes(self):
        for text in ['Mañana presentaré el dashboard al equipo de créditos.',
                     'Todavía tengo pendiente presentar el dashboard.',
                     'No pude presentar el dashboard.', 'No llegué a presentar el dashboard.',
                     'Estoy preparando la presentación para Créditos y Cobranzas.',
                     'La presentación sigue pendiente.', 'Si terminé el dashboard, presentaré mañana.',
                     'Juan dijo "Ya presenté el dashboard".', 'Ya tuve pendiente presentar el dashboard.']:
            with self.subTest(text=text):
                repo = CompletionRepo(text)
                answer, llm = self.complete(repo)
                self.assertEqual(answer, '')
                llm.assert_not_called()
                self.assertFalse(repo.changes)

    def test_ambiguity_lists_readable_options_and_never_mutates(self):
        repo = CompletionRepo()
        other = Row(**(vars(repo.task) | {'id': uuid4(), 'title': 'Presentar el dashboard a Créditos.'}))
        repo.tasks.append(other)
        decision = proposal(repo.task.id, alternatives=[{'task_id': str(other.id), 'confidence': .75}])
        answer, _ = self.complete(repo, decision)
        self.assertIn('¿Cuál quieres completar?', answer)
        self.assertIn(TITLE, answer)
        self.assertIn(other.title, answer)
        self.assertFalse(repo.changes)
        self.assertEqual(repo.task.status, 'open')
        self.assertEqual(other.status, 'open')

    def test_unknown_ids_low_confidence_and_ungrounded_evidence_do_not_mutate(self):
        for overrides in [{'matched_task_id': str(uuid4())}, {'confidence': .94},
                          {'evidence': 'Ya terminé una tarea inventada'},
                          {'completion_detected': False}, {'reason_code': 'no_completion'},
                          {'alternatives': [{'task_id': str(uuid4()), 'confidence': .99}]}]:
            with self.subTest(overrides=overrides):
                repo = CompletionRepo()
                self.complete(repo, proposal(repo.task.id, **overrides))
                self.assertEqual(repo.task.status, 'open')
                self.assertFalse(repo.changes)

    def test_evidence_cannot_strip_negation_or_future_from_its_original_clause(self):
        task_id = uuid4()
        candidates = [{'task_id': str(task_id)}]
        text = 'No presenté el dashboard. Ya envié otro informe.'
        self.assertTrue(has_completion_signal(text))
        decision = proposal(task_id, 'presenté el dashboard')
        self.assertEqual(validate_proposal(decision, text, candidates)[0], 'no_completion')

    def test_scope_prioritizes_project_company_and_area_without_cross_project_guess(self):
        repo = CompletionRepo()
        other = Row(**(vars(repo.project) | {'id': uuid4(), 'name': 'Otro proyecto', 'slug': 'otro-proyecto', 'aliases': []}))
        ids, error = completion_scope(TEXT, [repo.project, other], [repo.company], [repo.area])
        self.assertEqual(ids, [repo.project.id])
        self.assertIsNone(error)
        sql = candidate_statement(ids).compile(dialect=postgresql.dialect())
        self.assertIn(ids, sql.params.values())
        self.assertIn('tasks.project_id IN', str(sql))
        self.assertIn('tasks.completed_at IS NULL', str(sql))
        self.assertIn('tasks.processing_run_id = sources.latest_processing_run_id', str(sql))
        self.assertIn(['open', 'in_progress'], sql.params.values())
        ids, error = completion_scope('Ya terminé la presentación de Catusita', [repo.project, other],
                                      [repo.company], [repo.area])
        self.assertEqual(set(ids), {repo.project.id, other.id})
        unknown = Row(id=uuid4(), name='Primax', slug='primax')
        self.assertEqual(completion_scope(TEXT + ' Primax', [repo.project], [repo.company, unknown],
                                         [repo.area])[1], 'scope_conflict')

    def test_ambiguous_project_and_candidate_overflow_skip_provider(self):
        repo = CompletionRepo()
        repo.projects.append(Row(**(vars(repo.project) | {'id': uuid4()})))
        answer, llm = self.complete(repo)
        self.assertIn('proyecto inequívoco', answer)
        llm.assert_not_called()
        repo = CompletionRepo()
        repo.tasks = [Row(**(vars(repo.task) | {'id': uuid4()})) for _ in range(51)]
        answer, llm = self.complete(repo)
        self.assertIn('muchas tareas', answer)
        llm.assert_not_called()
        self.assertFalse(repo.changes)

    def test_duplicate_success_and_no_match_are_cached_without_new_call_or_event(self):
        for decision_kind in ['completed', 'no_match', 'ambiguous']:
            with self.subTest(decision_kind=decision_kind):
                repo = CompletionRepo()
                decision = proposal(repo.task.id, reason_code=decision_kind)
                first, _ = self.complete(repo, decision)
                second, llm = self.complete(repo, decision)
                self.assertEqual(first, second)
                llm.assert_not_called()
                self.assertEqual(len(repo.attempts), 1)
                self.assertEqual(len(repo.changes), int(decision_kind == 'completed'))

    def test_already_completed_or_stale_task_never_creates_change(self):
        repo = CompletionRepo()
        repo.task.status, repo.task.completed_at = 'completed', NOW
        _, llm = self.complete(repo)
        llm.assert_not_called()
        self.assertFalse(repo.changes)
        repo = CompletionRepo()
        repo.selected_missing = True
        self.complete(repo)
        self.assertFalse(repo.changes)
        self.assertEqual(repo.attempts[0].result['outcome'], 'changed_candidate')

    def test_changes_during_model_call_are_revalidated_under_lock(self):
        repo = CompletionRepo()
        def change_during_call(*args):
            repo.task.title = 'Otra tarea totalmente distinta'
            return proposal(repo.task.id)
        with patch('app.services.task_completion.propose_completion', side_effect=change_during_call):
            complete_from_note(repo, repo.source.id, settings())
        self.assertFalse(repo.changes)
        self.assertEqual(repo.task.status, 'open')
        self.assertEqual(repo.attempts[0].result['outcome'], 'changed_candidate')
        task_lock = next(s for s in repo.statements if s.column_descriptions[0]['entity'] is Task
                         and s._for_update_arg is not None)
        self.assertIsNotNone(task_lock._for_update_arg)

    def test_new_competing_candidate_during_model_call_aborts_completion(self):
        repo = CompletionRepo()
        def insert_during_call(*args):
            repo.tasks.append(Row(**(vars(repo.task) | {'id': uuid4()})))
            return proposal(repo.task.id)
        with patch('app.services.task_completion.propose_completion', side_effect=insert_during_call):
            complete_from_note(repo, repo.source.id, settings())
        self.assertFalse(repo.changes)

    def test_provider_error_keeps_note_and_caches_safe_failure(self):
        from app.services.reasoning_llm import ReasoningError
        repo = CompletionRepo()
        with patch('app.services.task_completion.propose_completion', side_effect=ReasoningError('safe')):
            answer = complete_from_note(repo, repo.source.id, settings())
        self.assertIn('ninguna tarea cambió', answer)
        self.assertEqual(repo.source.raw_content, TEXT)
        self.assertFalse(repo.changes)
        self.assertEqual(repo.attempts[0].result['outcome'], 'unavailable')

    def test_memory_change_marks_dirty_and_keeps_task_and_note_references(self):
        repo = CompletionRepo()
        self.complete(repo, cfg=settings(PROJECT_MEMORY_ENABLED=True))
        self.assertTrue(repo.state.is_dirty)
        self.assertEqual(repo.state.change_revision, 1)
        self.assertEqual(len(repo.events), 1)
        event = repo.events[0]
        self.assertEqual(event.event_type, 'task_change')
        self.assertEqual(event.origin_key, 'task:' + str(repo.changes[0].id))
        self.assertEqual(event.command_source_id, repo.source.id)
        self.assertEqual(event.source_id, repo.task.source_id)
        self.assertEqual(event.task_id, repo.task.id)
        self.complete(repo, cfg=settings(PROJECT_MEMORY_ENABLED=True))
        self.assertEqual(len(repo.events), 1)

    def test_pending_memory_delta_exposes_natural_completion_and_current_state(self):
        repo = CompletionRepo()
        self.complete(repo, cfg=settings(PROJECT_MEMORY_ENABLED=True))
        session = MagicMock()
        version = Row(id=uuid4(), version_number=1, memory={}, through_revision=0, consolidated_through_at=NOW)
        session.execute.side_effect = [Row(all=lambda: [(repo.state, version, repo.project.name)]), Row(all=lambda: [])]
        session.scalars.return_value.all.return_value = repo.events
        session.get.side_effect = lambda model, identifier: repo.changes[0] if model is TaskChange else repo.task
        _, deltas, _ = retrieve_project_memories(session, Scope([repo.project.id], repo.project.name),
                                                settings(PROJECT_MEMORY_ENABLED=True))
        delta = next(d for d in deltas if d.get('task_id') == str(repo.task.id))
        self.assertEqual(delta['action'], 'natural_completion')
        self.assertEqual(delta['after']['status'], 'completed')
        self.assertEqual(delta['current_task']['status'], 'completed')
        self.assertEqual(delta['source_id'], str(repo.source.id))

    def test_provider_uses_structured_output_and_only_candidates(self):
        repo = CompletionRepo()
        requests = []
        def transport(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [
                {'type': 'text', 'text': proposal(repo.task.id).model_dump_json()}]})
        candidates = [{'task_id': str(repo.task.id), 'title': TITLE}]
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            result = propose_completion(TEXT, candidates, settings(), client)
        self.assertEqual(result.matched_task_id, repo.task.id)
        payload = requests[0]
        self.assertEqual(payload['output_config']['format']['type'], 'json_schema')
        data = json.loads(payload['messages'][0]['content'])
        self.assertEqual(data['candidates'], candidates)
        self.assertNotIn('history', data)
        self.assertNotIn('tools', payload)

    def test_memory_refresh_snapshot_and_version_include_natural_completion(self):
        repo = CompletionRepo()
        self.complete(repo, cfg=settings(PROJECT_MEMORY_ENABLED=True))
        repo.events[0].created_at = NOW
        session = MagicMock()
        def get(model, identifier):
            return {ProjectMemoryState: repo.state, Project: repo.project,
                    TaskChange: repo.changes[0]}.get(model)
        session.get.side_effect = get
        session.scalars.side_effect = [Row(all=lambda: repo.events), Row(all=lambda: [repo.task]), Row(all=lambda: [])]
        session.execute.side_effect = [Row(all=lambda: []), Row(all=lambda: [])]
        cfg = settings(PROJECT_MEMORY_ENABLED=True)
        captured, data = snapshot_data(session, repo.project.id, cfg)
        self.assertEqual(data['tasks'][0]['status'], 'completed')
        self.assertEqual(data['changes'][0]['action'], 'natural_completion')
        self.assertEqual(data['changes'][0]['after']['status'], 'completed')
        self.assertEqual(data['changes'][0]['command_source_id'], str(repo.source.id))
        session.scalar.return_value = repo.state
        publish_memory(session, repo.project.id, captured, data,
                       {'current_state': {'text': 'Presentación completada.',
                                          'source_ids': [str(repo.source.id)]}}, cfg, False, NOW)
        version = session.add.call_args.args[0]
        self.assertEqual(version.through_revision, 1)
        self.assertEqual(version.retrieved_context['task_ids'], [str(repo.task.id)])
        self.assertFalse(repo.state.is_dirty)

    def test_note_completion_and_new_task_preserve_normal_extraction(self):
        text = 'Ya presenté el dashboard al equipo de créditos. Me pidieron agregar un filtro por cliente para el jueves.'
        repo = CompletionRepo(text)
        self.complete(repo)
        session = MagicMock()
        session.scalar.side_effect = lambda s: None if 'task_changes' in str(s) else repo.source
        session.scalars.return_value.all.return_value = [repo.project]
        data = {'project_id': str(repo.project.id), 'summary': 'Agregar filtro solicitado.',
            'tasks': [{'title': 'Agregar un filtro por cliente', 'description': None, 'owner_text': None,
                       'due_at': None, 'evidence': 'Me pidieron agregar un filtro por cliente para el jueves.'}],
            'decisions': [], 'people': [], 'dates': [], 'follow_ups': [], 'tags': []}
        with patch('app.services.processing.extract', return_value=Extraction.model_validate(data)) as extract:
            process_text_source(session, repo.source.id, settings())
        self.assertEqual(repo.task.status, 'completed')
        self.assertEqual(repo.source.raw_content, text)
        self.assertEqual(extract.call_args.args[1].raw_content, text)
        added = [call.args[0] for call in session.add.call_args_list]
        self.assertTrue(any(isinstance(item, Task) and item.title == 'Agregar un filtro por cliente' for item in added))

    def test_intent_keeps_natural_completion_as_note_and_commands_as_queries(self):
        self.assertEqual(deterministic_intent(TEXT + ' Marca esa tarea como completada.'), 'new_information')
        self.assertEqual(deterministic_intent('/completar ' + str(uuid4())), 'query')
        self.assertEqual(deterministic_intent('/ask ¿Qué presenté?'), 'query')

    def test_route_saves_source_then_completes_without_skipping_index_or_extraction(self):
        cfg = settings()
        app.dependency_overrides[get_settings] = lambda: cfg
        app.dependency_overrides[get_session] = lambda: MagicMock()
        source_id = str(uuid4())
        payload = {'update_id': 1, 'message': {'message_id': 1, 'date': 1700000000,
            'from': {'id': 123, 'is_bot': False}, 'chat': {'id': 123, 'type': 'private'}, 'text': TEXT}}
        order = []
        def ingest(*args, **kwargs):
            order.append('saved')
            return {'status': 'saved', 'source_id': source_id}
        def process(*args, **kwargs):
            order.append('interpreted')
            return {'source_id': source_id, 'action_plan': True, 'completed_titles': [TITLE],
                'new_task_titles': ['Agregar filtro'], 'tasks_count': 1, 'updates_count': 1}
        with patch('app.services.message_handling.ingest_update', side_effect=ingest) as saved, patch(
            'app.services.message_handling.process_source', side_effect=process), patch(
            'app.services.message_handling.maybe_index') as index, patch(
            'app.services.task_completion.complete_from_note') as old_completion:
            response = TestClient(app).post('/telegram/updates', json=payload, headers={'Authorization': 'Bearer fake'})
        self.assertEqual(order, ['saved', 'interpreted'])
        self.assertTrue(saved.call_args.kwargs['interpret'])
        self.assertEqual(response.json()['status'], 'answered')
        self.assertIn(TITLE, response.json()['answer'])
        index.assert_called_once()
        old_completion.assert_not_called()
        telegram = MagicMock()
        with patch('app.telegram.forward_update', return_value=response.json()), patch('app.telegram.process_saved_source') as process:
            process_update(MagicMock(), telegram, 'http://localhost', 'fake', 123, payload, processing=True)
        process.assert_not_called()
        self.assertIn(TITLE, telegram.call.call_args.args[1]['text'])

    def test_migration_is_additive_and_attempts_are_unique_and_immutable(self):
        buffer = io.StringIO()
        with patch('app.config.get_settings', return_value=settings(DATABASE_URL='postgresql://offline/test')):
            command.upgrade(Config('alembic.ini', output_buffer=buffer),
                            '0010_documents_jobs:0011_task_completion_attempts', sql=True)
        sql = buffer.getvalue()
        self.assertIn('CREATE TABLE task_completion_attempts', sql)
        self.assertIn('UNIQUE (source_id)', sql)
        self.assertIn('BEFORE UPDATE OR DELETE', sql)
        for forbidden in ['ALTER TABLE tasks', 'ALTER TABLE task_changes', 'UPDATE tasks', 'DROP TABLE', 'DELETE FROM']:
            self.assertNotIn(forbidden, sql)
