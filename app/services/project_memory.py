"""Bounded snapshots and optimistic publication; originals and old versions remain."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from uuid import UUID, uuid4
from sqlalchemy import select, text, or_, and_
from sqlalchemy.orm import Session
from app.models import (Project, ProjectMemoryState, ProjectMemoryVersion, ProjectMemoryEvent,
                        Source, Task, Decision, TaskChange, ProjectUpdate)
from app.services.queries import Scope, current_derived, source_statement, decision_statement
from app.services.project_memory_llm import (INCREMENTAL_PROMPT, RECONCILIATION_PROMPT,
                                            generate_memory)
from app.services.reasoning_llm import ReasoningError
from app.services.source_revisions import revision_delta
from app.services.embeddings import embedding_ready, EmbeddingError
from app.services.memory import semantic_search


def due_reconciliation(state, settings, now):
    local = now.astimezone(ZoneInfo(settings.project_memory_timezone))
    scheduled = local.replace(hour=settings.project_memory_reconciliation_hour, minute=0, second=0, microsecond=0)
    if local < scheduled or state.change_revision <= state.reconciled_revision or (state.last_change_at and state.last_change_at > scheduled):
        return False
    return state.last_reconciliation_at is None or state.last_reconciliation_at.astimezone(local.tzinfo) < scheduled


def candidates(session, settings, now, limit=50):
    local = now.astimezone(ZoneInfo(settings.project_memory_timezone))
    scheduled = local.replace(hour=settings.project_memory_reconciliation_hour, minute=0, second=0, microsecond=0)
    dirty = and_(ProjectMemoryState.is_dirty.is_(True), ProjectMemoryState.refresh_after <= now)
    nightly = and_(ProjectMemoryState.change_revision > ProjectMemoryState.reconciled_revision,
                   or_(ProjectMemoryState.last_change_at.is_(None), ProjectMemoryState.last_change_at <= scheduled),
                   or_(ProjectMemoryState.last_reconciliation_at.is_(None), ProjectMemoryState.last_reconciliation_at < scheduled)) if local >= scheduled else False
    states = session.scalars(select(ProjectMemoryState).join(Project).where(
        Project.status == "active", Project.archived_at.is_(None),
        or_(ProjectMemoryState.retry_after.is_(None), ProjectMemoryState.retry_after <= now),
        or_(dirty, nightly)).order_by(ProjectMemoryState.refresh_after.asc().nulls_last(), ProjectMemoryState.project_id).limit(limit)).all()
    return [(state.project_id, due_reconciliation(state, settings, now)) for state in states
            if not (state.retry_after and state.retry_after > now) and ((state.is_dirty and state.refresh_after and state.refresh_after <= now)
            or due_reconciliation(state, settings, now))][:limit]


def snapshot_data(session, project_id, settings, reconciliation=False):
    state = session.get(ProjectMemoryState, project_id)
    if state is None:
        raise ReasoningError("Project memory no esta programada.")
    project = session.get(Project, project_id)
    previous = session.get(ProjectMemoryVersion, state.current_version_id) if state.current_version_id else None
    through = previous.through_revision if previous else state.reconciled_revision
    changes = session.scalars(select(ProjectMemoryEvent).where(
        ProjectMemoryEvent.project_id == project_id, ProjectMemoryEvent.revision > through,
        ProjectMemoryEvent.revision <= state.change_revision).order_by(ProjectMemoryEvent.revision)
        .limit(settings.project_memory_incremental_max_sources)).all()
    cursor = changes[-1].revision if changes else through
    source_ids = list(dict.fromkeys(event.source_id for event in changes if event.source_id))
    scope = Scope([project_id], project.name)
    if source_ids:
        statement = source_statement(scope).where(Source.id.in_(source_ids))
    elif previous and not reconciliation:
        statement = None
    else:
        statement = source_statement(scope)
    rows = session.execute(statement.order_by(Source.received_at.desc(), Source.id)
        .limit(settings.project_memory_incremental_max_sources + 1)).all() if statement is not None else []
    sources = [{"source_id": str(source.id), "source_type": source.source_type,
                "received_at": source.received_at.isoformat(), "run_id": str(run.id) if run else None,
                "summary": (run.result.get("summary", "") if run else "")[:6000],
                "summary_truncated": bool(run and len(run.result.get("summary", "")) > 6000),
                "excerpt": source.raw_content[:1500], "excerpt_truncated": len(source.raw_content) > 1500}
               for source, run in rows[:settings.project_memory_incremental_max_sources]]
    tasks = session.scalars(select(Task).outerjoin(Source, Source.id == Task.source_id).where(
        Task.project_id == project_id, current_derived(Task)).order_by(Task.updated_at.desc(), Task.id).limit(61)).all()
    task_data = [{"task_id": str(task.id), "source_id": str(task.source_id) if task.source_id else None,
                  "title": task.title, "status": task.status, "owner": task.owner_text,
                  "due_at": task.due_at.isoformat() if task.due_at else None,
                  "completed_at": task.completed_at.isoformat() if task.completed_at else None}
                 for task in tasks[:60]]
    decisions = session.execute(decision_statement(scope).order_by(Decision.created_at.desc(), Decision.id).limit(41)).all()
    decision_data = [{"decision_id": str(item.id), "source_id": str(item.source_id) if item.source_id else None,
                      "text": item.decision_text[:1500], "text_truncated": len(item.decision_text) > 1500,
                      "decided_at": item.decided_at.isoformat() if item.decided_at else None}
                     for item, _ in decisions[:40]]
    event_data = []
    update_statement = select(ProjectUpdate).join(Source, Source.id == ProjectUpdate.source_id).where(
        ProjectUpdate.project_id == project_id, current_derived(ProjectUpdate))
    if source_ids and not reconciliation:
        update_statement = update_statement.where(ProjectUpdate.source_id.in_(source_ids))
    updates = session.scalars(update_statement.order_by(ProjectUpdate.created_at.desc(), ProjectUpdate.id).limit(41)).all()
    update_data = [{"update_id": str(u.id), "source_id": str(u.source_id), "text": u.update_text[:1500],
                   "text_truncated": len(u.update_text) > 1500,
                   "event_at": u.event_at.isoformat() if u.event_at else None} for u in updates[:40]]
    for event in changes:
        item = {"revision": event.revision, "event_type": event.event_type,
                "source_id": str(event.source_id) if event.source_id else None,
                "task_id": str(event.task_id) if event.task_id else None,
                "command_source_id": str(event.command_source_id) if event.command_source_id else None}
        if event.event_type == "source_revision":
            item.update(revision_delta(session,event))
        if event.event_type == "task_change":
            change = session.get(TaskChange, UUID(event.origin_key.split(":", 1)[1]))
            if change:
                item.update(before=change.before, after=change.after, action=change.action)
        event_data.append(item)
    data = {"project": {"id": str(project.id), "name": project.name},
            "previous_memory": previous.memory if previous else None,
            "sources": sources, "tasks": task_data, "decisions": decision_data, "updates": update_data,
            "changes": event_data, "chunks": [], "warnings": []}
    if len(rows) > settings.project_memory_incremental_max_sources or len(tasks) > 60 or len(decisions) > 40 or len(updates) > 40:
        data["warnings"].append("Recuperacion limitada; no implica toda la historia.")
    if reconciliation and source_ids:
        history = session.execute(source_statement(scope).where(Source.id.not_in(source_ids))
            .order_by(Source.received_at.desc(), Source.id).limit(settings.project_memory_incremental_max_sources)).all()
        data["history_sources"] = [{"source_id": str(source.id), "received_at": source.received_at.isoformat(),
            "summary": (run.result.get("summary", "") if run else "")[:3000], "excerpt": source.raw_content[:1000],
            "truncated": len(source.raw_content) > 1000} for source, run in history]
    if reconciliation and embedding_ready(settings):
        try:
            data["chunks"] = semantic_search(session, "objetivos avances riesgos bloqueos decisiones " + project.name,
                settings, project_ids=[project_id], limit=8)
        except EmbeddingError:
            data["warnings"].append("Reconciliacion sin semantic retrieval; conservada evidencia estructurada.")
    # Evidence JSON budget is independent from output memory budget; never truncate silently.
    if len(json.dumps(data, ensure_ascii=False)) > settings.reasoning_context_max_chars:
        # Remove lowest-priority historical chunks/old excerpts before considering a smaller batch.
        for key in ("chunks", "history_sources", "sources"):
            while data.get(key) and len(json.dumps(data, ensure_ascii=False)) > settings.reasoning_context_max_chars:
                if key == "sources" and source_ids:
                    raise ReasoningError("Batch incremental excede presupuesto; reduce PROJECT_MEMORY_INCREMENTAL_MAX_SOURCES.")
                data[key].pop()
                data["warnings"] = ["Evidencia historica reducida por presupuesto; cobertura parcial."]
        if len(json.dumps(data, ensure_ascii=False)) > settings.reasoning_context_max_chars:
            raise ReasoningError("Estado estructurado excede presupuesto seguro; ajusta configuracion.")
    captured = SimpleNamespace(previous_id=state.current_version_id, revision=state.change_revision,
        cursor=cursor, version_number=previous.version_number + 1 if previous else 1,
        cutoff=changes[-1].created_at if changes else datetime.now(timezone.utc))
    return captured, data


def memory_trace(data):
    return {"source_ids": [item["source_id"] for item in data["sources"]],
            "history_source_ids": [item["source_id"] for item in data.get("history_sources", [])],
            "task_ids": [item["task_id"] for item in data["tasks"]],
            "decision_ids": [item["decision_id"] for item in data["decisions"]],
            "update_ids": [item["update_id"] for item in data.get("updates", [])],
            "chunk_ids": [item["chunk_id"] for item in data["chunks"]],
            "event_revisions": [item["revision"] for item in data["changes"]],
            "warnings": data["warnings"]}


def publish_memory(session, project_id, captured, data, memory, settings, reconciliation, now):
    state = session.scalar(select(ProjectMemoryState).where(ProjectMemoryState.project_id == project_id)
                           .with_for_update().execution_options(populate_existing=True))
    if state.current_version_id != captured.previous_id:
        raise ReasoningError("La version de memoria cambio; reintenta con contexto nuevo.")
    _, model = settings.llm_credentials()
    version = ProjectMemoryVersion(id=uuid4(), project_id=project_id, version_number=captured.version_number,
        previous_version_id=captured.previous_id, memory=memory,
        update_type="reconciliation" if reconciliation else "incremental",
        trigger_source_ids=list(dict.fromkeys(item["source_id"] for item in data["sources"])),
        retrieved_context=memory_trace(data), through_revision=captured.cursor,
        consolidated_through_at=captured.cutoff, provider="anthropic", model=model,
        prompt_version=RECONCILIATION_PROMPT if reconciliation else INCREMENTAL_PROMPT)
    session.add(version)
    session.flush()
    state.retry_after = None
    state.current_version_id = version.id
    state.is_dirty = state.change_revision > captured.cursor
    if state.is_dirty:
        if state.change_revision == captured.revision:
            state.refresh_after = now
        # Newer events keep their own debounce deadline.
    else:
        state.dirty_since = None
        state.refresh_after = None
    if reconciliation:
        state.last_reconciliation_at = now
        state.reconciled_revision = captured.cursor
    else:
        state.last_incremental_at = now
    return version.id


def refresh_project(engine, project_id, settings, reconciliation=False, force=False, now=None):
    if settings.project_memory_enabled is not True:
        return False
    clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    now = clock()
    captured = None
    failure_guard = None
    published = False
    lock_id = int.from_bytes(hashlib.sha256(("project-memory:" + str(project_id)).encode()).digest()[:8], "big", signed=True)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            if not connection.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}):
                return False
            try:
                with Session(engine) as session:
                    with session.begin():
                        state = session.get(ProjectMemoryState, project_id)
                        if state is None:
                            return False
                        if not force and state.retry_after and state.retry_after > now:
                            return False
                        if not force and not (reconciliation and due_reconciliation(state, settings, now)) and not (
                            state.is_dirty and state.refresh_after and state.refresh_after <= now):
                            return False
                        failure_guard = (state.current_version_id, state.change_revision)
                        captured, data = snapshot_data(session, project_id, settings, reconciliation)
                    has_evidence = any(data.get(key) for key in ("sources", "tasks", "decisions", "updates", "chunks")) or bool(data.get("history_sources")) or bool(data["previous_memory"])
                    if not has_evidence:
                        with session.begin():
                            state = session.scalar(select(ProjectMemoryState).where(ProjectMemoryState.project_id == project_id)
                                .with_for_update().execution_options(populate_existing=True))
                            if state.change_revision == captured.revision and state.current_version_id == captured.previous_id:
                                state.is_dirty = state.change_revision > captured.cursor
                                if not state.is_dirty:
                                    state.dirty_since = None
                                state.retry_after = None
                                state.refresh_after = clock() if state.is_dirty else None
                                state.reconciled_revision = captured.cursor
                        return False
                    memory = generate_memory(settings, data, reconciliation)
                    connection.scalar(text("SELECT 1"))  # Lost ownership connection aborts publication.
                    with session.begin():
                        publish_memory(session, project_id, captured, data, memory, settings, reconciliation, clock())
                    published = True
                print(("Project memory reconciliation completed: " if reconciliation else "Project memory refreshed: ") + str(project_id), flush=True)
                return True
            finally:
                try:
                    connection.scalar(text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id})
                except Exception:
                    # A pooled session must never retain a lock after a failed unlock.
                    connection.invalidate()
    except Exception:
        if published:
            return True
        print("Project memory refresh failed: " + str(project_id), flush=True)
        try:
            with Session(engine) as session:
                with session.begin():
                    state = session.scalar(select(ProjectMemoryState).where(ProjectMemoryState.project_id == project_id)
                        .with_for_update().execution_options(populate_existing=True))
                    if state and failure_guard == (state.current_version_id, state.change_revision):
                        state.retry_after = clock() + timedelta(seconds=60)
        except Exception:
            pass
        return False
