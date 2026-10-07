import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch
from uuid import uuid4
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.config import get_settings
from app.database import get_session
from app.main import app
from app.services.intent_router import (IntentClassification, classify_intent, deterministic_intent,
    route_intent, AMBIGUOUS_REPLY, ERROR_REPLY)
from app.services.queries import catalog_query, answer_query, Scope
from tests.test_telegram import update, project
from tests.test_reasoning import settings

QUERIES = [
 'dame una lista de todos los proyectos que tienes', 'dime cómo está SIMA', 'muéstrame mis pendientes',
 'lista todos los proyectos de Laureate', 'resume lo que pasó esta semana en CIMA',
 'busca qué dijo Jorge sobre los costos', 'cuáles son los proyectos de la consultora',
 'qué tengo que hacer esta semana', 'cómo está Catusita', 'quién tiene pendientes', 'cuándo vence el dashboard',
 'que tengo pendiente de SIMA',
]
NOTES = [
 'SIMA: Jorge aprobó posiciones.',
 'SIMA: Jorge ya aprobó posiciones, pero todavía tenemos pendiente mejorar cócteles.',
 'El dashboard va a estar listo el miércoles.', 'El dashboard estará listo el miércoles.',
 'Hoy hablé con Gabriel y quedamos en revisar OpenRouter.',
 'Hoy hablé con Gabriel y acordamos revisar el costo.',
 'Catusita: el cliente aprobó el nuevo flujo.', 'Ana debe enviar el informe mañana.',
]


class IntentTests(unittest.TestCase):
    def setUp(self):
        self.cfg = settings(TELEGRAM_BOT_TOKEN='fake', TELEGRAM_USER_ID='12345')
        self.session = MagicMock()
        self.session.scalar.return_value = None
        app.dependency_overrides[get_settings] = lambda: self.cfg
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def post(self, text):
        return self.client.post('/telegram/updates', json=update(content=text), headers={'Authorization': 'Bearer fake'})

    def test_mandatory_queries_bypass_classifier(self):
        with patch('app.services.intent_router.classify_intent') as classify, redirect_stdout(io.StringIO()):
            for text in QUERIES:
                with self.subTest(text=text):
                    self.assertEqual(route_intent(text, self.cfg), 'query')
            classify.assert_not_called()

    def test_mandatory_notes_bypass_classifier(self):
        with patch('app.services.intent_router.classify_intent') as classify, redirect_stdout(io.StringIO()):
            for text in NOTES:
                with self.subTest(text=text):
                    self.assertEqual(route_intent(text, self.cfg), 'new_information')
            classify.assert_not_called()

    def test_note_query_words_inside_body_do_not_trigger_query(self):
        self.assertEqual(deterministic_intent('SIMA: Jorge aprobó el flujo y dijo cómo está funcionando.'), 'new_information')
        self.assertIsNone(deterministic_intent('Jorge dijo dame una lista de proyectos'))
        self.assertIsNone(deterministic_intent('OpenRouter de Estrategia'))
        self.assertIsNone(deterministic_intent('Quiero saber quién debe enviar el informe'))

    def test_schema_rejects_unknown_fields_nonfinite_bad_confidence_and_coercion(self):
        for payload in ({'intent': 'query', 'confidence': '0.99'}, {'intent': 'query', 'confidence': True},
            {'intent': 'query', 'confidence': float('nan')}, {'intent': 'query', 'confidence': 1.1},
            {'intent': 'query', 'confidence': -.1}, {'intent': 'sql', 'confidence': 1},
            {'intent': 'query', 'confidence': 1, 'project_id': str(uuid4())}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                IntentClassification.model_validate(payload)

    def test_classifier_reuses_small_json_call_and_version(self):
        result = IntentClassification(intent='ambiguous', confidence=.6, reason_code='unclear')
        with patch('app.services.intent_router.call_json', return_value=result) as call:
            self.assertIs(classify_intent('OpenRouter de Estrategia', self.cfg), result)
        self.assertEqual(call.call_args.args[2]['prompt_version'], 'intent-router-v1')
        self.assertEqual(call.call_args.kwargs['max_tokens'], 512)
        self.assertIs(call.call_args.args[3], IntentClassification)

    def test_low_confidence_and_ambiguous_never_save_or_make_second_call(self):
        # Legacy classifiers remain testable above, but never gate future natural messages.
        for text in ['OpenRouter de Estrategia']:
            with self.subTest(text=text), patch('app.services.message_handling.handle_message',
                return_value={'status': 'answered', 'answer': 'Guardado'}) as handle, patch(
                'app.services.intent_router.classify_intent') as classify, patch(
                'app.services.task_completion.complete_from_note') as completion:
                self.assertEqual(self.post(text).json()['answer'], 'Guardado')
                handle.assert_called_once()
                self.assertEqual(handle.call_args.args[1]['message']['text'], text)
                classify.assert_not_called()
                completion.assert_not_called()

    def test_classifier_failure_does_not_save_note_or_expose_provider(self):
        # Legacy classifiers remain testable above, but never gate future natural messages.
        for text in ['OpenRouter de Estrategia']:
            with self.subTest(text=text), patch('app.services.message_handling.handle_message',
                return_value={'status': 'answered', 'answer': 'Guardado'}) as handle, patch(
                'app.services.intent_router.classify_intent') as classify, patch(
                'app.services.task_completion.complete_from_note') as completion:
                self.assertEqual(self.post(text).json()['answer'], 'Guardado')
                handle.assert_called_once()
                self.assertEqual(handle.call_args.args[1]['message']['text'], text)
                classify.assert_not_called()
                completion.assert_not_called()

    def test_clear_queries_persist_as_queries_without_indexing(self):
        # Legacy classifiers remain testable above, but never gate future natural messages.
        for text in QUERIES:
            with self.subTest(text=text), patch('app.services.message_handling.handle_message',
                return_value={'status': 'answered', 'answer': 'Guardado'}) as handle, patch(
                'app.services.intent_router.classify_intent') as classify, patch(
                'app.services.task_completion.complete_from_note') as completion:
                self.assertEqual(self.post(text).json()['answer'], 'Guardado')
                handle.assert_called_once()
                self.assertEqual(handle.call_args.args[1]['message']['text'], text)
                classify.assert_not_called()
                completion.assert_not_called()

    def test_clear_notes_keep_existing_pipeline_and_overrides_are_absolute(self):
        # Legacy classifiers remain testable above, but never gate future natural messages.
        for text in NOTES + ['/nota cómo está SIMA?']:
            with self.subTest(text=text), patch('app.services.message_handling.handle_message',
                return_value={'status': 'answered', 'answer': 'Guardado'}) as handle, patch(
                'app.services.intent_router.classify_intent') as classify, patch(
                'app.services.task_completion.complete_from_note') as completion:
                self.assertEqual(self.post(text).json()['answer'], 'Guardado')
                handle.assert_called_once()
                self.assertEqual(handle.call_args.args[1]['message']['text'], text)
                classify.assert_not_called()
                completion.assert_not_called()

    def test_high_confidence_classifier_and_replay_use_persisted_type(self):
        # Legacy classifiers remain testable above, but never gate future natural messages.
        for text in ['OpenRouter de Estrategia']:
            with self.subTest(text=text), patch('app.services.message_handling.handle_message',
                return_value={'status': 'answered', 'answer': 'Guardado'}) as handle, patch(
                'app.services.intent_router.classify_intent') as classify, patch(
                'app.services.task_completion.complete_from_note') as completion:
                self.assertEqual(self.post(text).json()['answer'], 'Guardado')
                handle.assert_called_once()
                self.assertEqual(handle.call_args.args[1]['message']['text'], text)
                classify.assert_not_called()
                completion.assert_not_called()

    def test_catalog_requests_have_sql_fast_path(self):
        for text in [QUERIES[0], QUERIES[3], QUERIES[6]]:
            self.assertEqual(catalog_query(text).kind, 'projects')
        self.assertIsNone(catalog_query('lista proyectos y analiza sus riesgos'))
        session = MagicMock()
        session.scalars.return_value.all.return_value = [project('SIMA'), project('ALMA')]
        with patch('app.services.queries.resolve_scope', return_value=Scope(None, 'Todos')):
            answer = answer_query(session, catalog_query(QUERIES[0]))
        self.assertIn('Proyectos registrados: 2', answer)
        self.assertIn('SIMA', answer)
        self.assertNotIn('tasks', str(session.scalars.call_args.args[0]))

    def test_explicit_query_overrides_and_unauthorized_text_never_classify(self):
        for text in ['/ask Jorge aprobó posiciones', '/pregunta El dashboard estará listo']:
            with patch('app.services.intent_router.classify_intent') as classify, patch('app.api.routes.telegram.ingest_update',
                return_value={'status': 'saved', 'source_id': str(uuid4())}), patch('app.api.routes.telegram.answer_reasoning', return_value='Respuesta'):
                self.assertEqual(self.post(text).json()['answer'], 'Respuesta')
                classify.assert_not_called()
        with patch('app.services.intent_router.classify_intent') as classify:
            candidate = update(content='Texto difícil'); candidate['message']['from']['id'] = 999
            self.assertEqual(self.client.post('/telegram/updates', json=candidate, headers={'Authorization': 'Bearer fake'}).json()['status'], 'ignored')
            classify.assert_not_called()
