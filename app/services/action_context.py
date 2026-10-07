"""Bounded, application-owned interpretation context; no language classification."""
import json
from sqlalchemy import select
from app.models import Task, Source, ProjectUpdate
from app.services.queries import current_derived


def assemble_context(session, source, projects, settings, include_message=True):
    catalog = [{"id": str(p.id), "name": p.name, "slug": p.slug,
        "aliases": [a.alias for a in p.aliases],
        "area": p.area.name if p.area else None, "company": p.company.name if p.company else None}
        for p in projects]
    if len(catalog) > 100:
        raise ValueError("Catalog too large for safe interpretation.")
    tasks = session.scalars(select(Task).outerjoin(Source, Source.id == Task.source_id).where(
        current_derived(Task), Task.status.in_(["open", "in_progress"]), Task.completed_at.is_(None),
        Task.project_id.in_([p.id for p in projects])).order_by(Task.updated_at.desc(), Task.id).limit(101)).all()
    candidates = [{"id": str(t.id), "project_id": str(t.project_id), "title": t.title,
                   "description": (t.description or "")[:1000], "owner": t.owner_text,
                   "due_at": t.due_at.isoformat() if t.due_at else None} for t in tasks[:100]]
    updates = session.scalars(select(ProjectUpdate).join(Source, Source.id == ProjectUpdate.source_id).where(
        current_derived(ProjectUpdate), ProjectUpdate.project_id.in_([p.id for p in projects]))
        .order_by(ProjectUpdate.created_at.desc()).limit(10)).all()
    message = source.raw_metadata.get("message") or source.raw_metadata.get("edited_message") or {}
    context = {"message": source.raw_content if include_message else None, "filename": message.get("document", {}).get("file_name") or
        source.raw_metadata.get("filename"), "original_date_unix": message.get("date"),
        "received_at": source.received_at.isoformat(), "timezone": "America/Lima", "projects": catalog,
        "open_tasks": candidates, "tasks_complete": len(tasks) <= 100,
        "recent_updates": [{"project_id": str(u.project_id), "text": u.update_text[:1000],
                            "source_id": str(u.source_id)} for u in updates],
        "command_mode": source.raw_metadata.get("command_mode")}
    context["document_scope"] = {"project_id": str(source.primary_project_id) if source.primary_project_id else None,
                                 "caption": message.get("caption")} if message.get("document") else None
    # Drop supplementary context, never silently omit completion alternatives.
    while len(json.dumps(context, ensure_ascii=False)) > settings.reasoning_context_max_chars and context["open_tasks"]:
        context["open_tasks"].pop()
        context["tasks_complete"] = False
    if len(json.dumps(context, ensure_ascii=False)) > settings.reasoning_context_max_chars:
        context["recent_updates"] = []
    if len(json.dumps(context, ensure_ascii=False)) > settings.reasoning_context_max_chars:
        raise ValueError("Interpretation context exceeds safe budget.")
    return context


def assemble_v2_context(session, source, projects, settings, include_message=True):
    from app.services.project_candidates import candidate_projects
    from uuid import UUID
    recent = session.scalars(select(ProjectUpdate).join(Source, Source.id == ProjectUpdate.source_id)
        .where(current_derived(ProjectUpdate)).order_by(ProjectUpdate.created_at.desc()).limit(10)).all()
    catalog, truncated = candidate_projects(projects, source.raw_content,
        recent_ids=[u.project_id for u in recent], pinned_id=source.primary_project_id,
        max_chars=min(20000, settings.reasoning_context_max_chars // 3))
    message = source.raw_metadata.get("message") or source.raw_metadata.get("edited_message") or {}
    context = {"schema_version": "action-plan-v2", "message": source.raw_content if include_message else None,
        "filename": source.raw_metadata.get("filename") or message.get("document", {}).get("file_name"),
        "original_date_unix": message.get("date"), "received_at": source.received_at.isoformat(),
        "timezone": "America/Lima", "projects": catalog, "candidate_projects": [],
        "catalog_truncated": truncated, "command_mode": source.raw_metadata.get("command_mode"),
        "document_scope": {"project_id": str(source.primary_project_id) if source.primary_project_id else None,
                           "caption": message.get("caption")} if message.get("document") else None,
        "recent_updates": [{"project_id": str(u.project_id), "text": u.update_text[:1000],
                            "source_id": str(u.source_id)} for u in recent if str(u.project_id) in {p["id"] for p in catalog}]}
    for project in catalog:
        tasks = session.scalars(select(Task).outerjoin(Source, Source.id == Task.source_id).where(
            current_derived(Task), Task.status.in_(["open", "in_progress"]), Task.completed_at.is_(None),
            Task.project_id == UUID(project["id"])).order_by(Task.updated_at.desc(), Task.id).limit(101)).all()
        context["candidate_projects"].append({"project_id": project["id"], "tasks_complete": len(tasks) <= 100,
            "open_tasks": [{"id": str(t.id), "project_id": str(t.project_id), "title": t.title,
                "description": (t.description or "")[:1000], "owner": t.owner_text,
                "due_at": t.due_at.isoformat() if t.due_at else None} for t in tasks[:100]]})
    # Remove supplementary updates first, then task rows with explicit per-project incompleteness.
    if len(json.dumps(context, ensure_ascii=False)) > settings.reasoning_context_max_chars:
        context["recent_updates"] = []
    while len(json.dumps(context, ensure_ascii=False)) > settings.reasoning_context_max_chars:
        groups = [g for g in context["candidate_projects"] if g["open_tasks"]]
        if not groups:
            raise ValueError("Interpretation context exceeds safe budget.")
        group = max(groups, key=lambda g: len(json.dumps(g, ensure_ascii=False)))
        group["open_tasks"].pop()
        group["tasks_complete"] = False
    return context
