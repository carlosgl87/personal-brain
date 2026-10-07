"""Transactional execution of validated plans, shared by short and long sources."""
from datetime import datetime, timezone
from uuid import UUID, uuid4
from sqlalchemy import select
from app.models import (Task, Source, Project, Decision, ProjectUpdate, ProcessingRun, TaskChange,
                        TaskEvidence, DecisionEvidence, UpdateEvidence)
from app.services.queries import current_derived
from app.services.task_management import snapshot, record_task_change
from app.services.project_memory_events import mark_dirty
from app.services.message_interpreter import validate_plan
from app.services.claude import ExtractionError


def execute_plan(session, source, plan, context, settings, prompt_version, provenance=None, metadata=None):
    if plan.schema_version == "action-plan-v2":
        from app.services.action_execution_v2 import execute_v2
        return execute_v2(session, source, plan, context, settings, prompt_version, provenance, metadata)
    validate_plan(plan, source, context)
    project_id = plan.project_id if plan.scope_confidence >= .90 else None
    # An explicit document caption is authoritative, including unresolved captions.
    from app.services.project_matching import allows_content_project
    if not allows_content_project(source):
        project_id = source.primary_project_id
    if project_id is not None:
        if str(project_id) not in {p["id"] for p in context["projects"]}:
            raise ExtractionError("El proyecto fijado ya no está en el catálogo activo.")
        active = session.scalar(select(Project.id).where(Project.id == project_id,
            Project.status == "active", Project.archived_at.is_(None)).with_for_update())
        if active is None:
            raise ExtractionError("El proyecto dejó de estar activo durante la interpretación.")
    by_id = {t["id"]: t for t in context["open_tasks"]}
    completion_status, completed, ambiguities = {}, [], list(plan.ambiguities)
    if project_id is None and (plan.project_id is not None or plan.completed_tasks) and not ambiguities:
        ambiguities.append("Precisa el proyecto para poder aplicar las completions con seguridad.")
    eligible = []
    material_ambiguity = bool(plan.ambiguities) or any(t.alternatives for t in plan.completed_tasks)
    for item in plan.completed_tasks:
        candidate = by_id[str(item.task_id)]
        valid = (not material_ambiguity and context["tasks_complete"] and project_id is not None
                 and candidate["project_id"] == str(project_id) and item.confidence >= .95
                 and item.state == "performed")
        completion_status[str(item.task_id)] = "proposed" if valid else "not_applied"
        if valid:
            eligible.append(item)
        elif item.state == "performed" and not material_ambiguity:
            ambiguities.append("No pude confirmar con seguridad la completion de «" + candidate["title"] + "». Precisa el pendiente.")
    # Lock original sources in a consistent order before target tasks.
    task_ids = sorted([item.task_id for item in eligible], key=str)
    originals = session.scalars(select(Task.source_id).where(Task.id.in_(task_ids))).all() if task_ids else []
    for original_id in sorted({i for i in originals if i and i != source.id}, key=str):
        session.scalar(select(Source).where(Source.id == original_id).with_for_update())
    locked = session.scalars(select(Task).outerjoin(Source, Source.id == Task.source_id).where(
        Task.id.in_(task_ids), current_derived(Task), Task.status.in_(["open", "in_progress"]),
        Task.completed_at.is_(None)).order_by(Task.id).with_for_update(of=Task)
        .execution_options(populate_existing=True)).all() if task_ids else []
    locked_by_id = {t.id: t for t in locked}
    # Cancel the entire completion set if a selected candidate changed during interpretation.
    changed = len(locked) != len(task_ids) or any(
        str(t.project_id) != by_id[str(t.id)]["project_id"] or t.title != by_id[str(t.id)]["title"]
        or (t.description or "")[:1000] != by_id[str(t.id)]["description"]
        or t.owner_text != by_id[str(t.id)]["owner"]
        or (t.due_at.isoformat() if t.due_at else None) != by_id[str(t.id)]["due_at"] for t in locked)
    if changed:
        eligible = []
        ambiguities.append("Las tareas cambiaron durante la interpretación; no se completó ninguna.")
    if eligible:
        # The project lock also blocks concurrent FK insertions of new competing tasks.
        current_ids = session.scalars(select(Task.id).outerjoin(Source, Source.id == Task.source_id).where(
            current_derived(Task), Task.project_id == project_id, Task.status.in_(["open", "in_progress"]),
            Task.completed_at.is_(None)).limit(101)).all()
        supplied = {UUID(t["id"]) for t in context["open_tasks"] if t["project_id"] == str(project_id)}
        if set(current_ids) != supplied:
            eligible = []
            ambiguities.append("Los pendientes disponibles cambiaron durante la interpretación; precisa qué tarea completar.")
    _, model = settings.llm_credentials()
    run = ProcessingRun(id=uuid4(), source_id=source.id, provider="anthropic", model=model,
                        prompt_version=prompt_version, result={})
    session.add(run)
    session.flush()
    for kind, klass, evidence_class, fk in (("tasks", Task, TaskEvidence, "task_id"),
        ("decisions", Decision, DecisionEvidence, "decision_id"),
        ("updates", ProjectUpdate, UpdateEvidence, "update_id")):
        seen = set()
        for index, item in enumerate(getattr(plan, kind)):
            values = item.model_dump(exclude={"evidence"})
            signature = tuple(str(values[k]) for k in sorted(values))
            if signature in seen:
                continue
            seen.add(signature)
            if kind == "tasks":
                values["status"] = "open"
            row = klass(id=uuid4(), source_id=source.id, project_id=project_id, processing_run_id=run.id, **values)
            session.add(row)
            session.flush()
            if provenance:
                for loc in provenance[kind][index]:
                    session.add(evidence_class(id=uuid4(), **{fk: row.id},
                        source_chunk_id=UUID(loc["source_chunk_id"]), processing_run_part_id=UUID(loc["processing_run_part_id"]),
                        generation_part_id=UUID(loc["generation_part_id"]) if loc.get("generation_part_id") else None,
                        evidence=loc["evidence"], char_start=loc["char_start"], char_end=loc["char_end"]))
    for item in eligible:
        task = locked_by_id[item.task_id]
        previous = session.scalar(select(TaskChange).where(TaskChange.command_source_id == source.id, TaskChange.task_id == task.id))
        if previous is not None:
            completion_status[str(task.id)] = "already_applied"
            continue
        before = snapshot(task)
        task.status, task.completed_at = "completed", datetime.now(timezone.utc)
        record_task_change(session, task, source.id, "natural_completion", before,
                           "✅ Completada: " + task.title, settings)
        completed.append(task.title)
        completion_status[str(task.id)] = "completed"
    if material_ambiguity and plan.completed_tasks:
        choices = list(dict.fromkeys(by_id[str(i)]["title"] for t in plan.completed_tasks
                                    for i in [t.task_id, *t.alternatives]))
        ambiguities.append("Precisa qué tarea quieres completar: " + "; ".join(choices[:5]))
    run.result = plan.model_dump(mode="json") | (metadata or {}) | {"execution": {
        "project_id": str(project_id) if project_id else None, "completed_titles": completed,
        "completion_status": completion_status, "ambiguities": ambiguities}}
    source.primary_project_id, source.latest_processing_run_id = project_id, run.id
    source.processing_status, source.processed_at = "processed", datetime.now(timezone.utc)
    if plan.interaction != "query":
        mark_dirty(session, project_id, settings, origin_key="run:" + str(run.id), source_id=source.id,
                   event_type="project_update" if plan.updates else "processed_source")
    return run
