import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.seed import seed


class SeedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path('app/seed_data.json').read_text(encoding='utf-8'))
        self.projects = {p['name']: p for p in self.data['projects']}

    def test_requested_aliases_and_preserved_existing_aliases(self):
        expected = {
            'AI Prospector': ['IA Prospectora', 'Prospector', 'Agente Prospector'],
            'Self-Service Admissions Flow': ['Flujo de Admisión', 'Admisión Self-Service',
                                             'Flujo Admision Online', 'Flujo de Admision Online'],
            'AI Super Advisor': ['Asesor de Asesores', 'Super Advisor', 'ADA', 'Asesor Asesores'],
            'AI Tutor': ['Tutor IA', 'Tutor AI', 'AVA', 'Ciby', 'Cibi', 'ABA'],
            'Engagement / Re-enrollment Model': ['Engagement Model', 'Re-enrollment Model',
                'Modelo de Reinscripción', 'Modelo de Engagement', 'Modelo de Rematricula'],
            'Early Dropout / No-show Model': ['Early Dropout', 'No-show Model', 'Modelo No Show',
                                              'Modelo Zombie', 'Zombie Model'],
            'FrontRunner': ['Frontrunner', 'Front Runner', 'Frontrunner McKinsey', 'McKinsey Frontrunner'],
            'AI Class Insights': ['Class Audit Platform', 'QA Videos', 'Class Insights'],
            'AI Educational Content Generator': ['Content Generation Platform',
                'Plataforma de Creación de Contenido', 'Plataforma de Generación de Contenidos',
                'Plataforma Generación Contenidos', 'Plataforma de Generacion de Contenidos',
                'Plataforma Generacion Contenidos'],
            'GCP Migration & Landing Zone': ['GCP Migration', 'Landing Zone', 'GCP Landing Zone',
                'Migracion Google', 'Google Migracion', 'Google Migration', 'Migracion GCP'],
            'Operating Model / McKinsey': ['Operating Model', 'McKinsey', 'Modelo Operativo',
                                           'Modelo Operativo McKinsey'],
            'Catu - Agente Vendedores': ['Catu', 'Agente Vendedores', 'Agente de Vendedores',
                'Agente Catusita', 'Catusita Agente Vendedores', 'Catusita Agente de Vendedores'],
            'Autogestión Reclamos': ['Autogestion Reclamos', 'Autogestión de Reclamos',
                'Reclamos Financiera Oh', 'Agente Reclamos', 'Agente de Reclamos'],
        }
        for name, aliases in expected.items():
            with self.subTest(project=name):
                self.assertTrue(set(aliases).issubset(self.projects[name]['aliases']))

    def test_administrative_projects_have_explicit_scope_and_no_generic_aliases(self):
        for area in ['Laureate', 'Consultora']:
            with self.subTest(area=area):
                p = self.projects['Administración ' + area]
                self.assertEqual(p['area'], area)
                self.assertEqual(p['slug'], area.lower() + '-administrativo')
                self.assertEqual(p['category'], 'Administrativo' if area == 'Laureate' else None)
                self.assertIsNone(p['company'])
                self.assertEqual(p['aliases'], ['Administrativo ' + area, 'Admin ' + area,
                                                'Administracion ' + area])
                self.assertFalse({'Administración', 'Administrativo', 'Admin'} & set(p['aliases']))

    def test_seed_maps_administrative_catalog_to_nullable_foreign_keys(self):
        rows = {}
        def ensure_row(session, model, keys, values):
            key = (model.__tablename__, tuple(keys.items()))
            return rows.setdefault(key, SimpleNamespace(id=uuid4(), **keys, **values))
        with patch('app.seed.ensure', side_effect=ensure_row):
            seed(MagicMock())
        by_table = {}
        for (table, _), row in rows.items():
            by_table.setdefault(table, []).append(row)
        for area in ['Laureate', 'Consultora']:
            p = next(row for row in by_table['projects'] if row.name == 'Administración ' + area)
            area_row = next(row for row in by_table['areas'] if row.name == area)
            self.assertEqual(p.area_id, area_row.id)
            self.assertIsNone(p.company_id)
            if area == 'Laureate':
                category = next(row for row in by_table['categories'] if row.name == 'Administrativo')
                self.assertEqual(p.category_id, category.id)
            else:
                self.assertIsNone(p.category_id)
