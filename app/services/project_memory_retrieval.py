"""Memory accelerates reasoning; pending events remain visible during debounce."""
from uuid import UUID
from sqlalchemy import select
from app.models import ProjectMemoryState, ProjectMemoryVersion, ProjectMemoryEvent, Source, Project, TaskChange, Task
from app.services.project_memory_llm import memory_references
from app.services.source_revisions import revision_delta
from app.services.queries import Scope, scoped, source_statement


def retrieve_project_memories(session, scope, settings, limit=30):
    if settings.project_memory_enabled is not True:
        return [], [], []
    rows = session.execute(scoped(select(ProjectMemoryState, ProjectMemoryVersion, Project.name)
        .join(ProjectMemoryVersion, ProjectMemoryVersion.id == ProjectMemoryState.current_version_id)
        .join(Project, Project.id == ProjectMemoryState.project_id)
        .where(Project.status == "active", Project.archived_at.is_(None)),
        ProjectMemoryState.project_id, scope).order_by(ProjectMemoryVersion.created_at.desc()).limit(limit + 1)).all()
    memories, deltas, warnings = [], [], []
    if len(rows) > limit:
        warnings.append("Project Memories limitadas por profundidad y presupuesto.")
    for state, version, name in rows[:limit]:
        content = version.memory
        if scope.project_ids is None:
            content = {key: {**section, "text": section["text"][:350], "truncated": len(section["text"]) > 350}
                for key, section in content.items() if key in {"executive_summary", "current_state", "risks_and_blockers", "open_items", "recent_changes"}}
        memories.append({"memory_version_id": str(version.id), "project_id": str(state.project_id),
            "project": name, "version_number": version.version_number, "memory": content,
            "source_ids": sorted(memory_references(content)), "is_dirty": state.is_dirty or state.change_revision > version.through_revision,
            "consolidated_through_at": version.consolidated_through_at.isoformat(),
            "through_revision": version.through_revision})
        if state.is_dirty or state.change_revision > version.through_revision:
            events = session.scalars(select(ProjectMemoryEvent).where(
                ProjectMemoryEvent.project_id == state.project_id,
                ProjectMemoryEvent.revision > version.through_revision
            ).order_by(ProjectMemoryEvent.revision.desc()).limit(16)).all()
            ids = list(dict.fromkeys(e.source_id for e in events if e.source_id))
            if ids:
                source_rows = session.execute(source_statement(Scope([state.project_id], name)).where(Source.id.in_(ids))
                    .order_by(Source.received_at.desc()).limit(15)).all()
                for source, run in source_rows:
                    deltas.append({"source_id": str(source.id), "project_id": str(state.project_id),
                        "received_at": source.received_at.isoformat(), "run_id": str(run.id) if run else None,
                        "summary": (run.result.get("summary", "") if run else "")[:3000],
                        "excerpt": source.raw_content[:2000], "truncated": len(source.raw_content) > 2000})
            for event in events:
                if event.event_type == "source_revision":
                    deltas.append(revision_delta(session,event))
                if event.command_source_id:
                    change = session.get(TaskChange, UUID(event.origin_key.split(":", 1)[1])) if event.event_type == "task_change" else None
                    task = session.get(Task, event.task_id) if event.task_id else None
                    deltas.append({"before": change.before if change else None, "after": change.after if change else None,
                                   "current_task": {"title": task.title, "completed_at": task.completed_at.isoformat() if task.completed_at else None, "status": task.status, "project_id": str(task.project_id) if task.project_id else None,
                                       "owner": task.owner_text, "due_at": task.due_at.isoformat() if task.due_at else None} if task else None,
                                   "source_id": str(event.command_source_id), "project_id": str(state.project_id),
                                   "task_id": str(event.task_id), "event_type": event.event_type,
                                   "action": change.action if change else None,
                                   "note": "Cambio de tarea posterior a la memoria; prioriza estado SQL actual."})
            warnings.append("Memoria de " + name + " pendiente de actualizacion; se incorpora delta y estado SQL vigente.")
            if len(events) > 15:
                warnings.append("Delta limitado a eventos recientes; cobertura parcial.")
    return memories, deltas, warnings
