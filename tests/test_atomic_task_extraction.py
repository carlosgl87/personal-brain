import copy
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as Row
from unittest.mock import patch
from uuid import uuid4

import httpx

from app.models import Task, TaskEvidence, ProcessingRun
from app.schemas.extraction import Extraction, ExtractedTask
from app.schemas.hierarchical import ConsolidatedExtraction
from app.services.claude import extract, SYSTEM_PROMPT, PROMPT_VERSION
from app.services.hierarchical_llm import (extract_part, consolidation_input, consolidate,
    PART_PROMPT_VERSION, CONSOLIDATION_PROMPT_VERSION, CONSOLIDATION_SYSTEM)
from app.services.processing import process_text_source
from app.services.task_completion import complete_from_note
from app.services.task_extraction_rules import TASK_ATOMICITY_RULES, CONSOLIDATION_ATOMICITY_RULES
from tests.test_hierarchical import FakeSession, config
from tests.test_task_completion import CompletionRepo, proposal


SEND = 'Enviar los correos a los responsables de IT'
CONSOLIDATE = 'Consolidar las respuestas de los responsables de IT'
DEPENDENCY = 'Realizar después de recibir las respuestas a los correos enviados.'


def task(title, evidence, **fields):
    return dict(title=title, evidence=evidence, description=None, owner_text=None, due_at=None) | fields


def extraction(tasks, project_id=None):
    return dict(project_id=str(project_id) if project_id else None, summary='Pendientes de trabajo.',
                tasks=tasks, decisions=[], people=[], dates=[], follow_ups=[], tags=[])


class AtomicTaskExtractionTests(unittest.TestCase):
    def provider_extract(self, text, tasks):
        source = Row(raw_content=text, raw_metadata={'message': {'date': 1791302400}},
                     received_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc))
        requests = []
        def transport(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [
                {'type': 'text', 'text': json.dumps(extraction(tasks), ensure_ascii=False)}]})
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            result = extract(config(), source, [], client)
        return result, requests

    def test_multi_action_examples_retain_separate_tasks_with_shared_literal_evidence(self):
        cases = [
            ('Tengo que enviar los correos a los responsables de IT y consolidar sus respuestas.',
             [SEND, CONSOLIDATE]),
            ('Mañana debo enviar el dashboard a Luis y luego recoger su feedback.',
             ['Enviar el dashboard a Luis', 'Recoger el feedback de Luis']),
            ('Hay que corregir las observaciones y después poner la nueva versión en producción.',
             ['Corregir las observaciones', 'Poner la nueva versión en producción']),
            ('Agregar los usuarios al sistema y mandarles el correo con el link.',
             ['Agregar los usuarios al sistema', 'Enviar el correo con el link a los usuarios']),
            ('Validar la información con cada equipo, consolidar las correcciones y devolver la versión final a McKinsey.',
             ['Validar la información con cada equipo', 'Consolidar las correcciones',
              'Enviar la versión final validada a McKinsey']),
        ]
        for text, titles in cases:
            with self.subTest(text=text):
                result, requests = self.provider_extract(text, [task(title, text) for title in titles])
                self.assertEqual([t.title for t in result.tasks], titles)
                self.assertEqual([t.evidence for t in result.tasks], [text] * len(titles))
                self.assertEqual(len(requests), 1)
                self.assertIn(TASK_ATOMICITY_RULES, requests[0]['system'])
                self.assertEqual(json.loads(requests[0]['messages'][0]['content'])['source_text'], text)

    def test_single_action_examples_are_preserved_without_mechanical_splitting(self):
        for text in ['Revisar costos y presupuesto del proyecto', 'Validar nombre y correo de cada usuario',
                     'Revisar los datos de créditos y cobranzas', 'Preparar una presentación clara y ejecutiva',
                     'Confirmar si la orden de compra y el acta de conformidad pueden hacerse antes de la nueva firma del contrato']:
            with self.subTest(text=text):
                result, _ = self.provider_extract(text, [task(text, text)])
                self.assertEqual(len(result.tasks), 1)
                self.assertEqual(result.tasks[0].title, text)

    def test_date_only_applies_to_sending_and_dependency_survives_schema(self):
        text = 'Mañana tengo que enviar los correos y cuando respondan debo consolidar la información.'
        result, requests = self.provider_extract(text, [
            task('Enviar los correos', text, due_at='2026-10-07T17:00:00-05:00'),
            task('Consolidar las respuestas', text, description=DEPENDENCY)])
        self.assertEqual(result.tasks[0].due_at.isoformat(), '2026-10-07T17:00:00-05:00')
        self.assertIsNone(result.tasks[1].due_at)
        self.assertEqual(result.tasks[1].description, DEPENDENCY)
        data = json.loads(requests[0]['messages'][0]['content'])
        self.assertEqual(data['timezone'], 'America/Lima')
        self.assertEqual(data['original_date_unix'], 1791302400)

    def test_shared_deadline_can_be_retained_for_both_actions(self):
        text = 'Antes del viernes tengo que revisar los datos y enviar el informe.'
        due = '2026-10-09T17:00:00-05:00'
        result, _ = self.provider_extract(text, [task('Revisar los datos', text, due_at=due),
                                                task('Enviar el informe', text, due_at=due)])
        self.assertEqual([t.due_at.isoformat() for t in result.tasks], [due, due])

    def test_different_owners_are_preserved_per_action_without_combining(self):
        text = 'Yo enviaré el informe y Ana consolidará las respuestas.'
        result, _ = self.provider_extract(text, [task('Enviar el informe', text, owner_text='yo'),
                                                task('Consolidar las respuestas', text, owner_text='Ana')])
        self.assertEqual([t.owner_text for t in result.tasks], ['yo', 'Ana'])

    def test_short_processing_persists_two_independent_rows_and_marks_memory(self):
        text = 'Tengo que enviar los correos a los responsables de IT y consolidar sus respuestas.'
        repo = FakeSession(raw='SIMA. ' + text)
        original = repo.source.raw_content
        tasks = [task(SEND, text, owner_text='yo', due_at='2026-10-07T17:00:00-05:00'),
                 task(CONSOLIDATE, text, owner_text='Ana', description=DEPENDENCY)]
        result = Extraction.model_validate(extraction(tasks, repo.projects[0].id))
        with patch('app.services.processing.extract', return_value=result), patch(
                  'app.services.processing.mark_dirty') as dirty:
            response = process_text_source(repo, repo.source.id, config())
        rows = [row for row in repo.rows if isinstance(row, Task)]
        self.assertEqual([row.title for row in rows], [SEND, CONSOLIDATE])
        self.assertEqual([row.owner_text for row in rows], ['yo', 'Ana'])
        self.assertIsNotNone(rows[0].due_at)
        self.assertIsNone(rows[1].due_at)
        self.assertEqual(rows[1].description, DEPENDENCY)
        self.assertEqual(response['tasks_count'], 2)
        self.assertEqual(repo.source.raw_content, original)
        self.assertEqual({row.source_id for row in rows}, {repo.source.id})
        self.assertEqual({row.processing_run_id for row in rows}, {repo.source.latest_processing_run_id})
        run = next(row for row in repo.rows if isinstance(row, ProcessingRun))
        self.assertEqual(run.prompt_version, PROMPT_VERSION)
        dirty.assert_called_once()

    def partial_fixture(self):
        text = 'Tengo que enviar los correos a los responsables de IT y luego consolidar sus respuestas.'
        source = Row(id=uuid4(), raw_content=text, primary_project_id=None, source_type='document_text',
                     received_at=datetime.now(timezone.utc), raw_metadata={'filename': 'notas.txt'})
        chunk = Row(id=uuid4(), content=text, char_start=0, char_end=len(text))
        payload = extraction([task(SEND, text, owner_text='Ana'),
                              task(CONSOLIDATE, text, owner_text='Ana', description=DEPENDENCY)])
        calls = []
        def partial_call(settings, system, data, schema, client=None, **kwargs):
            calls.append((system, data, schema))
            return Extraction.model_validate(payload)
        with patch('app.services.hierarchical_llm.call_json', side_effect=partial_call):
            saved = extract_part(config(), source, chunk)
        part = Row(id=uuid4(), source_chunk_id=chunk.id, part_index=0, result=saved)
        return source, chunk, part, payload, calls

    def test_hierarchical_partial_receives_same_policy_and_keeps_evidence_for_each_action(self):
        source, chunk, part, _, calls = self.partial_fixture()
        self.assertIn(TASK_ATOMICITY_RULES, calls[0][0])
        self.assertEqual(calls[0][1]['source_text'], source.raw_content)
        self.assertEqual(len(part.result['extraction']['tasks']), 2)
        self.assertEqual(len(part.result['evidence']['tasks']), 2)
        for evidence in part.result['evidence']['tasks']:
            location = evidence[0]
            self.assertEqual(location['source_chunk_id'], str(chunk.id))
            self.assertEqual(source.raw_content[location['char_start']:location['char_end']], location['evidence'])

    def test_consolidation_preserves_two_actions_despite_same_quote_owner_and_project(self):
        source, _, part, payload, _ = self.partial_fixture()
        data, _ = consolidation_input(source, [part], [], config())
        final = copy.deepcopy(payload)
        for index, row in enumerate(final['tasks']):
            row['candidate_ids'] = [data['tasks'][index]['candidate_id']]
        final['dispositions'] = [dict(candidate_id=t['candidate_id'], status='kept',
                                      final_index=i, reason=None) for i, t in enumerate(data['tasks'])]
        with patch('app.services.hierarchical_llm.call_json', return_value=ConsolidatedExtraction.model_validate(final)) as call:
            result, provenance = consolidate(config(), source, [part], [])
        self.assertIn(CONSOLIDATION_ATOMICITY_RULES, call.call_args.args[1])
        self.assertEqual([t.title for t in result.tasks], [SEND, CONSOLIDATE])
        self.assertEqual(result.tasks[1].description, DEPENDENCY)
        self.assertEqual(len(provenance['tasks']), 2)
        self.assertEqual([d['final_index'] for d in provenance['dispositions']], [0, 1])

    def test_long_pipeline_creates_separate_tasks_with_separate_evidence_links(self):
        text = 'Tengo que enviar los correos a los responsables de IT y luego consolidar sus respuestas.'
        repo = FakeSession(raw='SIMA. ' + text + '\n' + 'Contenido adicional. ' * 1800)
        def model(settings, system, data, schema, client=None, **kwargs):
            if schema is Extraction:
                items = [task(SEND, text), task(CONSOLIDATE, text, description=DEPENDENCY)] if text in data['source_text'] else []
                return Extraction.model_validate(extraction(items, data['project_id']))
            final = extraction([], data['project_id'])
            final['tasks'] = [{k: v for k, v in row.items() if k not in ('candidate_id', 'provenance')}
                             | {'candidate_ids': [row['candidate_id']]} for row in data['tasks']]
            final['dispositions'] = [dict(candidate_id=row['candidate_id'], status='kept', final_index=i,
                                          reason=None) for i, row in enumerate(data['tasks'])]
            return ConsolidatedExtraction.model_validate(final)
        with patch('app.services.hierarchical_llm.call_json', side_effect=model), patch(
                  'app.services.hierarchical.mark_dirty') as dirty:
            response = process_text_source(repo, repo.source.id, config())
        rows = [row for row in repo.rows if isinstance(row, Task)]
        self.assertEqual([row.title for row in rows], [SEND, CONSOLIDATE])
        self.assertEqual(len({row.id for row in rows}), 2)
        links = [row for row in repo.rows if isinstance(row, TaskEvidence)]
        self.assertEqual({link.task_id for link in links}, {row.id for row in rows})
        self.assertEqual(response['tasks_count'], 2)
        run = next(row for row in repo.rows if isinstance(row, ProcessingRun))
        self.assertEqual(run.prompt_version, CONSOLIDATION_PROMPT_VERSION)
        self.assertEqual(run.result['partial_prompt_version'], PART_PROMPT_VERSION)
        dirty.assert_called_once()

    def test_natural_completion_of_sending_keeps_consolidation_open(self):
        text = 'Ya envié los correos de validación a los responsables de IT.'
        repo = CompletionRepo(text)
        repo.task.title = SEND
        waiting = Row(**(vars(repo.task) | {'id': uuid4(), 'title': CONSOLIDATE, 'description': DEPENDENCY}))
        repo.tasks.append(waiting)
        with patch('app.services.task_completion.propose_completion', return_value=proposal(repo.task.id, text)) as model:
            complete_from_note(repo, repo.source.id, config())
        self.assertEqual(repo.task.status, 'completed')
        self.assertEqual(waiting.status, 'open')
        self.assertIsNone(waiting.completed_at)
        self.assertEqual(len(repo.changes), 1)
        self.assertEqual({t['title'] for t in model.call_args.args[1]}, {SEND, CONSOLIDATE})

    def test_schema_describes_action_scope_and_prompts_have_new_versions(self):
        properties = ExtractedTask.model_json_schema()['properties']
        for field in ['title', 'description', 'due_at', 'owner_text']:
            self.assertIn('description', properties[field])
        self.assertIn(TASK_ATOMICITY_RULES, SYSTEM_PROMPT)
        self.assertIn(CONSOLIDATION_ATOMICITY_RULES, CONSOLIDATION_SYSTEM)
        self.assertNotEqual(PROMPT_VERSION, 'work-extraction-v1')
        self.assertNotEqual(PART_PROMPT_VERSION, 'meeting-part-v1')
        self.assertNotEqual(CONSOLIDATION_PROMPT_VERSION, 'meeting-consolidation-v2-dispositions')
