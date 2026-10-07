"""Per-item execution; callers own the source transaction and source lock."""
from datetime import datetime, timezone
from uuid import UUID, uuid4
from sqlalchemy import select
from app.models import (Task, Source, Project, Decision, ProjectUpdate, ProcessingRun, TaskChange,
                        TaskEvidence, DecisionEvidence, UpdateEvidence)
from app.services.queries import current_derived
from app.services.task_management import snapshot, record_task_change
from app.services.project_memory_events import mark_dirty
from app.services.message_interpreter import validate_plan
from app.services.project_matching import allows_content_project


def candidate_snapshot(task):
    return {"id": str(task.id), "project_id": str(task.project_id), "title": task.title,
            "description": (task.description or "")[:1000], "owner": task.owner_text,
            "due_at": task.due_at.isoformat() if task.due_at else None}


def execute_v2(session, source, plan, context, settings, prompt_version, provenance=None, metadata=None):
    validate_plan(plan, source, context)
    groups = {g["project_id"]: g for g in context["candidate_projects"]}
    candidates = {t["id"]: t for g in groups.values() for t in g["open_tasks"]}
    proposed_projects = {item.project_id for item in [*plan.tasks, *plan.decisions, *plan.updates,
                         *plan.completed_tasks] if item.project_id}
    # Canonical project locking also prevents new FK-linked competing tasks during execution.
    active = set()
    for pid in sorted(proposed_projects, key=str):
        if session.scalar(select(Project.id).where(Project.id == pid, Project.status == "active",
                Project.archived_at.is_(None)).with_for_update()) is not None:
            active.add(pid)

    def item_project(item):
        if item.project_id not in active or item.scope_confidence < .90:
            return None
        if any(a.type == "project" for a in item.ambiguities) or context.get("catalog_truncated"):
            return None
        if not allows_content_project(source) and item.project_id != source.primary_project_id:
            return None
        return item.project_id

    completion_reasons = {}
    eligible = []
    for item in plan.completed_tasks:
        reason = None
        if item_project(item) is None:
            reason = "unresolved_project"
        elif item.alternatives or any(a.type in {"project", "task", "other"} for a in item.ambiguities):
            reason = "ambiguous"
        elif not groups[str(item.project_id)]["tasks_complete"]:
            reason = "incomplete_candidates"
        elif item.confidence < .95:
            reason = "low_confidence"
        elif item.state != "performed":
            reason = "not_performed"
        if reason:
            completion_reasons[item.task_id] = reason
        else:
            eligible.append(item)
    ids = sorted([i.task_id for i in eligible], key=str)
    originals = session.scalars(select(Task.source_id).where(Task.id.in_(ids))).all() if ids else []
    for original in sorted({i for i in originals if i and i != source.id}, key=str):
        session.scalar(select(Source).where(Source.id == original).with_for_update())
    locked = session.scalars(select(Task).outerjoin(Source, Source.id == Task.source_id).where(
        Task.id.in_(ids), current_derived(Task), Task.status.in_(["open", "in_progress"]),
        Task.completed_at.is_(None)).order_by(Task.id).with_for_update(of=Task)
        .execution_options(populate_existing=True)).all() if ids else []
    by_id = {t.id: t for t in locked}
    changed_projects = set()
    for pid in sorted({i.project_id for i in eligible}, key=str):
        current = session.scalars(select(Task.id).outerjoin(Source, Source.id == Task.source_id).where(
            current_derived(Task), Task.project_id == pid, Task.status.in_(["open", "in_progress"]),
            Task.completed_at.is_(None)).limit(101)).all()
        supplied = {UUID(t["id"]) for t in groups[str(pid)]["open_tasks"]}
        if set(current) != supplied:
            changed_projects.add(pid)
    for item in eligible:
        task = by_id.get(item.task_id)
        if item.project_id in changed_projects:
            completion_reasons[item.task_id] = "candidates_changed"
        elif task is None or candidate_snapshot(task) != candidates[str(item.task_id)]:
            completion_reasons[item.task_id] = "task_changed"

    _, model = settings.llm_credentials()
    run = ProcessingRun(id=uuid4(), source_id=source.id, provider="anthropic", model=model,
                        prompt_version=prompt_version, result={})
    session.add(run)
    session.flush()
    execution, affected = [], set()
    for kind, klass, evidence_class, fk in (("tasks", Task, TaskEvidence, "task_id"),
        ("decisions", Decision, DecisionEvidence, "decision_id"),
        ("updates", ProjectUpdate, UpdateEvidence, "update_id")):
        seen = set()
        for index, item in enumerate(getattr(plan, kind)):
            pid = item_project(item)
            audit = {"type": kind, "index": index, "proposed_project_id": str(item.project_id) if item.project_id else None,
                     "project_id": str(pid) if pid else None, "status": "not_applied"}
            audit["adjustments"] = [a.type + "_left_unknown" for a in item.ambiguities if a.type in {"owner", "date"}]
            values = item.model_dump(exclude={"evidence", "project_id", "scope_confidence", "ambiguities"})
            # Ambiguous fields remain unknown; independent facts can still be saved unscoped.
            if any(a.type == "owner" for a in item.ambiguities) and kind == "tasks":
                values["owner_text"] = None
            if any(a.type == "date" for a in item.ambiguities):
                values[{"tasks": "due_at", "decisions": "decided_at", "updates": "event_at"}[kind]] = None
            signature = (pid, tuple(str(values[k]) for k in sorted(values)))
            if signature in seen:
                audit["reason"] = "duplicate_in_plan"
                execution.append(audit)
                continue
            seen.add(signature)
            if kind == "tasks":
                values["status"] = "open"
            row = klass(id=uuid4(), source_id=source.id, project_id=pid, processing_run_id=run.id, **values)
            session.add(row)
            session.flush()
            audit.update(status="applied", record_id=str(row.id), reason="unresolved_project" if pid is None else None)
            audit["title"] = values.get("title") or values.get("decision_text") or values.get("update_text")
            execution.append(audit)
            if pid:
                affected.add(pid)
            if provenance:
                for loc in provenance[kind][index]:
                    session.add(evidence_class(id=uuid4(), **{fk: row.id},
                        source_chunk_id=UUID(loc["source_chunk_id"]), processing_run_part_id=UUID(loc["processing_run_part_id"]),
                        generation_part_id=UUID(loc["generation_part_id"]) if loc.get("generation_part_id") else None,
                        evidence=loc["evidence"], char_start=loc["char_start"], char_end=loc["char_end"]))
    for index, item in enumerate(plan.completed_tasks):
        audit = {"type": "completed_task", "index": index, "task_id": str(item.task_id),
                 "project_id": str(item.project_id) if item.project_id else None,
                 "status": "not_applied", "title": candidates[str(item.task_id)]["title"],
                 "alternatives": [{"id": str(i), "title": candidates[str(i)]["title"]} for i in item.alternatives]}
        reason = completion_reasons.get(item.task_id)
        if reason:
            audit["reason"] = reason
        else:
            task = by_id[item.task_id]
            prior = session.scalar(select(TaskChange).where(TaskChange.command_source_id == source.id, TaskChange.task_id == task.id))
            if prior:
                audit.update(status="already_applied", reason="idempotent")
            else:
                before = snapshot(task)
                task.status, task.completed_at = "completed", datetime.now(timezone.utc)
                record_task_change(session, task, source.id, "natural_completion", before,
                                   "✅ Completada: " + task.title, settings)
                audit.update(status="completed", reason=None)
                affected.add(task.project_id)
        execution.append(audit)
    # Mutating sources have membership in every actually affected project, independently of query scope.
    primary = next(iter(affected)) if len(affected) == 1 else None
    run.result = plan.model_dump(mode="json") | (metadata or {}) | {"execution": {
        "project_id": str(primary) if primary else None, "project_ids": sorted(str(p) for p in affected),
        "items": execution, "completed_titles": [i["title"] for i in execution if i["status"] == "completed"],
        "completion_status": {i["task_id"]: i["status"] for i in execution if i["type"] == "completed_task"}}}
    source.primary_project_id, source.latest_processing_run_id = primary, run.id
    source.processing_status, source.processed_at = "processed", datetime.now(timezone.utc)
    for pid in sorted(affected, key=str):
        mark_dirty(session, pid, settings, origin_key="run:" + str(run.id) + ":project:" + str(pid),
                   source_id=source.id, event_type="project_update" if any(i["type"] == "updates" and
                   i["project_id"] == str(pid) and i["status"] == "applied" for i in execution) else "processed_source")
    return run
