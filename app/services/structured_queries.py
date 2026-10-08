"""Exact entity retrieval and presentation; no interpretation of user language."""
import json
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, func
from app.models import Task, Source, Project, Decision, Company, Area
from app.services.queries import current_derived, task_statement, decision_statement, scoped

ROW_LIMIT = 1000


def display_tasks(tasks):
    counts = defaultdict(int)
    shown = []
    for ordinal, task in enumerate(tasks, 1):
        pid = task.get('project_id')
        counts[pid] += 1
        shown.append({'task_id': task['task_id'], 'title': task['title'], 'project_id': pid,
            'project_name': task.get('project'), 'status': task['status'],
            'ordinal': ordinal, 'project_ordinal': counts[pid]})
    return shown


def retrieve_structured(session, plan, settings, scope):
    from app.services.reasoning import date_filter, temporal_column, clipped
    context = {'scope': scope.label, 'data_authority': 'structured', 'tasks': [], 'decisions': [],
        'catalog': [], 'catalog_entity': plan.catalog_entity, 'task_selection': 'all_current' if plan.include_completed_tasks else 'open', 'projects': [], 'updates': [],
        'recent_sources': [], 'chunks': [], 'project_memories': [], 'new_sources': [],
        'warnings': [], 'coverage': {}, 'semantic_coverage': {'requested': False, 'material': False},
        'temporal_window': {'time_basis': plan.time_basis,
            'date_from': plan.date_from.isoformat() if plan.date_from else None,
            'date_to': plan.date_to.isoformat() if plan.date_to else None}}
    definitions = []
    if plan.include_tasks:
        if plan.include_completed_tasks:
            statement = scoped(select(Task, Project.name).outerjoin(Project, Project.id == Task.project_id)
                .outerjoin(Source, Source.id == Task.source_id).where(current_derived(Task)), Task.project_id, scope)
        else:
            statement = task_statement(scope)
        statement = date_filter(statement, temporal_column('tasks', plan.time_basis), plan)
        definitions.append(('tasks', statement, (Project.name.asc().nulls_last(), Task.due_at.asc().nulls_last(), Task.created_at, Task.id)))
    if plan.include_decisions:
        statement = date_filter(decision_statement(scope), temporal_column('decisions', plan.time_basis), plan)
        definitions.append(('decisions', statement, (Project.name.asc().nulls_last(), Decision.created_at.desc(), Decision.id)))
    if plan.catalog_entity:
        model = {'projects': Project, 'companies': Company, 'areas': Area}[plan.catalog_entity]
        statement = select(model)
        if model is Project:
            statement = scoped(statement, Project.id, scope)
        elif scope.project_ids is not None:
            column = Project.company_id if model is Company else Project.area_id
            statement = statement.where(model.id.in_(scoped(select(column), Project.id, scope)))
        definitions.append(('catalog', statement, (model.name, model.id)))
    # Budget is for the complete returned dataset, not a silent prefix called "all".
    remaining = max(0, settings.reasoning_context_max_chars - 4000)
    for kind, statement, ordering in definitions:
        model = statement.column_descriptions[0]['entity']
        rows = session.execute(statement.add_columns(func.count(model.id).over().label('total_count'))
            .order_by(*ordering).limit(ROW_LIMIT)).all()
        total = rows[0][-1] if rows else 0
        for row in rows:
            if kind == 'tasks':
                task, name = row[:2]
                item = {'task_id': str(task.id), 'title': task.title, 'project_id': str(task.project_id) if task.project_id else None,
                    'project': name, 'status': task.status, 'completed_at': task.completed_at.isoformat() if task.completed_at else None,
                    'owner': task.owner_text, 'due_at': task.due_at.isoformat() if task.due_at else None,
                    'source_id': str(task.source_id) if task.source_id else None,
                    'run_id': str(task.processing_run_id) if task.processing_run_id else None,
                    'description': clipped(task.description, 500)}
            elif kind == 'decisions':
                decision, name = row[:2]
                item = {'decision_id': str(decision.id), 'text': clipped(decision.decision_text, 1500),
                    'project_id': str(decision.project_id) if decision.project_id else None, 'project': name,
                    'source_id': str(decision.source_id) if decision.source_id else None,
                    'run_id': str(decision.processing_run_id) if decision.processing_run_id else None,
                    'decided_at': decision.decided_at.isoformat() if decision.decided_at else None}
            else:
                entity = row[0]
                item = {'id': str(entity.id), 'name': entity.name, 'status': getattr(entity, 'status', None)}
            size = len(json.dumps(item, ensure_ascii=False))
            if size > remaining:
                break
            context[kind].append(item)
            remaining -= size
        shown = len(context[kind])
        context['coverage'][kind] = {'total': total, 'shown': shown, 'complete': shown == total,
            'reason': None if shown == total else 'safety_or_character_budget'}
    context['retrieval'] = {'budget_reduced': any(not c['complete'] for c in context['coverage'].values()),
        'counts': {key: len(context[key]) for key in ('tasks','decisions','catalog')},
        'context_max_chars': settings.reasoning_context_max_chars}
    return context


def date_label(value):
    return datetime.fromisoformat(value).astimezone(ZoneInfo('America/Lima')).strftime('%d/%m/%Y %H:%M')


def render_structured(context):
    sections = []
    labels = {'tasks': 'tareas abiertas' if context.get('task_selection') == 'open' else 'tareas registradas', 'decisions': 'decisiones registradas', 'catalog': {'projects': 'proyectos registrados', 'companies': 'empresas registradas', 'areas': 'áreas registradas'}.get(context.get('catalog_entity'), 'registros')}
    for kind, coverage in context['coverage'].items():
        noun = labels[kind]
        count, shown = coverage['total'], coverage['shown']
        if not count:
            sections.append('No hay ' + noun + ' en este alcance.')
            continue
        heading = (f'{count} {noun}.' if coverage['complete'] else f'Hay {count} {noun}. Te muestro las primeras {shown}.')
        if kind == 'catalog':
            sections.append(heading + '\n' + '\n'.join('• ' + item['name'] for item in context[kind]))
            continue
        groups = {}
        for item in context[kind]:
            text = item['title'] if kind == 'tasks' else item['text']['text']
            details = []
            if kind == 'tasks':
                if item.get('owner'): details.append(item['owner'])
                if item.get('due_at'): details.append('Vence ' + date_label(item['due_at']))
                if item.get('status') not in {'open', 'in_progress'}: details.append(item['status'])
            elif item.get('decided_at'): details.append(date_label(item['decided_at']))
            line = '• ' + text + (' — ' + ' | '.join(details) if details else '')
            if kind == 'decisions' and item['text']['truncated']: line += ' [texto abreviado]'
            groups.setdefault(item.get('project') or 'Sin proyecto explícito', []).append(line)
        sections.append(heading + '\n\n' + '\n\n'.join(name + '\n' + '\n'.join(lines) for name, lines in groups.items()))
    return '\n\n'.join(sections) or 'No se seleccionaron entidades para consultar. Precisa qué necesitas.'
