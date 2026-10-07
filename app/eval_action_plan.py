"""Optional paid Claude evaluation on synthetic contexts, without database access.

python -m app.eval_action_plan
python -m app.eval_action_plan --fixtures-only
"""
import argparse
import json
from datetime import datetime, timezone
from types import SimpleNamespace as Row
from uuid import UUID
from app.config import Settings
from app.services.project_candidates import candidate_projects
from app.services.message_interpreter import interpret, validate_plan

PROJECTS = [Row(id=UUID(int=i), name=name, slug=slug, aliases=[Row(alias=a) for a in aliases], area=None, company=None)
            for i, name, slug, aliases in [
                (1, 'Dashboard Cobranzas', 'dashboard-cobranzas', ['Cobranzas']),
                (2, 'Catu - Agente Vendedores', 'catu-agente-vendedores', ['Catu']),
                (3, 'Operating Model / McKinsey', 'operating-model-mckinsey', ['McKinsey']),
                (4, 'FrontRunner', 'frontrunner', ['McKinsey FrontRunner'])]]
TASKS = [dict(id=str(UUID(int=i)), project_id=str(UUID(int=p)), title=title, description='', owner=None, due_at=None)
         for i, p, title in [(101, 1, 'Enviar accesos'), (103, 3, 'Enviar correos de validación a IT'),
                             (104, 3, 'Terminar presentación')]]
CASES = [
    ('Dashboard Cobranzas', 'Ya tuvimos la reunión con Cobranzas. Los datos cuadraron y las líneas recomendadas les hicieron sentido. Mañana tengo que mandar los accesos.'),
    ('Catu', 'Para Catu quiero cambiar la validación. Si el ID no parece un número debe pedir DNI y nombre para saber si es vendedor o supervisor. Esto hay que implementarlo.'),
    ('McKinsey completion', 'Operating Model / McKinsey: ya envié los correos de validación a los responsables de IT.'),
    ('Dos completions', 'Operating Model / McKinsey: ya envié los correos y terminé la presentación.'),
    ('Futuro', 'Mañana enviaré los correos.'),
    ('Negación', 'No pude enviar los correos.'),
    ('Decision', 'Evaluamos ambas opciones y decidimos usar Databricks.'),
    ('Proposal', 'Tal vez deberíamos usar Databricks.'),
    ('Mixed', 'Ya envié los correos de IT. ¿Qué me queda pendiente de Operating Model / McKinsey?'),
    ('Multi-project', 'En Dashboard Cobranzas ya enviamos los accesos. Para Catu quiero incorporar la línea de crédito disponible dentro del agente.')]


def fixture(text):
    projects, truncated = candidate_projects(PROJECTS, text, recent_ids=[UUID(int=3)])
    return {'schema_version': 'action-plan-v2', 'message': text, 'projects': projects,
        'candidate_projects': [{'project_id': p['id'], 'tasks_complete': True,
            'open_tasks': [t for t in TASKS if t['project_id'] == p['id'] and not (text == CASES[0][1] and t['id'] == str(UUID(int=101))) ]} for p in projects],
        'catalog_truncated': truncated, 'received_at': '2026-10-07T15:00:00+00:00',
        'original_date_unix': 1791385200, 'timezone': 'America/Lima', 'command_mode': None,
        'document_scope': None, 'recent_updates': [], 'filename': None}


def expectations(index, plan):
    checks = []
    if index == 0:
        checks = [bool(plan.updates), bool(plan.tasks), not plan.decisions,
                  not plan.completed_tasks, all(t.project_id == UUID(int=1) for t in plan.tasks)]
    elif index == 1:
        checks = [bool(plan.tasks), all(t.project_id == UUID(int=2) for t in plan.tasks)]
    elif index in {2, 3, 8}:
        wanted = {UUID(int=103), UUID(int=104)} if index == 3 else {UUID(int=103)}
        checks = [{t.task_id for t in plan.completed_tasks if t.state == 'performed' and not t.alternatives} == wanted,
                  not plan.tasks, bool(plan.query) if index == 8 else plan.query is None]
    elif index in {4, 5}:
        checks = [not plan.completed_tasks, not plan.decisions]
    elif index == 6:
        checks = [bool(plan.decisions)]
    elif index == 7:
        checks = [not plan.decisions, not plan.tasks, not plan.completed_tasks]
    else:
        checks = [any(t.project_id == UUID(int=1) and t.task_id == UUID(int=101) for t in plan.completed_tasks),
                  any(t.project_id == UUID(int=2) for t in plan.tasks),
                  all(t.project_id == UUID(int=2) for t in plan.tasks)]
    return all(checks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures-only', action='store_true', help='No provider calls or credentials required.')
    args = parser.parse_args()
    settings = None
    if not args.fixtures_only:
        try:
            settings = Settings()
            settings.llm_credentials()
        except Exception:
            print('SKIP: no hay credencial/modelo local de Claude válido; no se abrió ninguna DB.')
            return 0
    review = 0
    for index, (label, text) in enumerate(CASES):
        context = fixture(text)
        if args.fixtures_only:
            print(json.dumps({'case': label, 'input': text, 'context': context}, ensure_ascii=False))
            continue
        try:
            plan = interpret(settings, context)
            validate_plan(plan, Row(raw_content=text), context)
            status = 'PASS' if expectations(index, plan) else 'REVIEW'
            review += status == 'REVIEW'
            print(json.dumps({'case': label, 'input': text, 'status': status, 'plan': plan.model_dump(mode='json')}, ensure_ascii=False), flush=True)
        except Exception:
            review += 1
            print(json.dumps({'case': label, 'input': text, 'status': 'REVIEW', 'reason': 'provider_or_validation_failed'}, ensure_ascii=False), flush=True)
    if not args.fixtures_only:
        print(f'{len(CASES) - review} PASS / {review} REVIEW; datos sintéticos, sin escrituras DB.')
    return 0 if not review else 1


if __name__ == '__main__':
    raise SystemExit(main())
