import unittest
from types import SimpleNamespace as Row
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.documents import document_scope
from app.services.normalization import slugify
from app.services.project_matching import resolve_document_project, match_source_project


def catalog(name):
    return Row(id=uuid4(), name=name, slug=slugify(name))


class DocumentMatchingTests(unittest.TestCase):
    def setUp(self):
        self.consultora, self.laureate = catalog('Consultora'), catalog('Laureate')
        self.oh, self.catusita = catalog('Financiera Oh'), catalog('Catusita')
        def project(name, area, company, aliases):
            return Row(id=uuid4(), name=name, slug=slugify(name), area_id=area.id,
                       company_id=company.id if company else None, area=area, company=company,
                       aliases=[Row(alias=a) for a in aliases])
        self.reclamos = project('Autogestión Reclamos', self.consultora, self.oh,
                                ['Agente Reclamos', 'Agente de Reclamos'])
        self.other = project('Agente Reclamos Catusita', self.consultora, self.catusita,
                             ['Agente Reclamos', 'Agente de Reclamos'])
        self.laureate_project = project('Agente Reclamos Laureate', self.laureate, None,
                                        ['Agente Reclamos', 'Agente de Reclamos'])
        self.catu = project('Catu - Agente Vendedores', self.consultora, self.catusita, ['Catu'])
        self.projects = [self.reclamos, self.other, self.laureate_project, self.catu]

    def resolve(self, filename, content='contenido genérico'):
        return resolve_document_project(filename, content, self.projects,
                                        [self.oh, self.catusita], [self.consultora, self.laureate])

    def test_required_filename_and_content_cases(self):
        cases = [
            ('Financiera_Oh_agente_reclamos.txt', '', self.reclamos),
            ('Catusita_agente_reclamos.txt', '', self.other),
            ('agente_reclamos.txt', 'contenido genérico', None),
            ('Laureate_agente_reclamos.txt', '', self.laureate_project),
            ('Financiera_Oh_agente_reclamos.txt', 'Catu - Agente Vendedores / Catusita', None),
            ('Financiera_Oh_notas.txt', 'El agente de reclamos...', self.reclamos),
            ('notas.txt', 'Financiera Oh ... agente de reclamos ...', self.reclamos),
            ('notas.txt', 'agente de reclamos...', None),
        ]
        for filename, content, expected in cases:
            with self.subTest(filename=filename, content=content):
                self.assertIs(self.resolve(filename, content), expected)

    def test_caption_wins_and_unknown_or_ambiguous_caption_stays_unassigned(self):
        session = MagicMock()
        session.scalars.return_value.all.return_value = self.projects
        for caption, expected in [('/proyecto Autogestión Reclamos', self.reclamos),
                                  ('Autogestión Reclamos', self.reclamos),
                                  ('Agente Reclamos', None), ('NoExiste', None)]:
            self.assertIs(document_scope(session, 'Catu', caption, 'Catusita_agente_reclamos.txt')[1], expected)
        self.assertEqual(session.scalars.call_count, 4)

    def test_combined_context_same_project_conflicts_and_boundaries(self):
        self.assertIs(self.resolve('Consultora_Financiera_Oh_agente_reclamos.txt'), self.reclamos)
        self.assertIs(self.resolve('Financiera_Oh_agente_reclamos.txt', 'Autogestión Reclamos'), self.reclamos)
        self.assertIsNone(self.resolve('Laureate_Financiera_Oh_agente_reclamos.txt'))
        self.assertIsNone(self.resolve('Financiera_Oh_notas.txt', 'Catu'))
        self.assertIsNone(self.resolve('Financiera_Oh_notas.txt', 'Catusita notas'))
        self.assertIsNone(self.resolve('notas.txt', 'Catusitas agente reclamos'))
        self.assertIsNone(self.resolve('Financiera_Oh_notas.txt', 'contenido genérico'))
        duplicate = Row(**vars(self.reclamos) | {'id': uuid4()})
        self.projects.append(duplicate)
        self.assertIsNone(self.resolve('Financiera_Oh_agente_reclamos.txt'))

    def test_later_processing_preserves_document_conflict(self):
        source = Row(raw_content='Catu', raw_metadata={'message': {'document': {
            'file_name': 'Financiera_Oh_notas.txt'}}})
        self.assertIsNone(match_source_project(source, self.projects))
        source.raw_content = 'El agente de reclamos'
        self.assertIs(match_source_project(source, self.projects), self.reclamos)
