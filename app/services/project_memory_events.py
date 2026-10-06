"""Mark changes in the same transaction as their source/task; no provider calls."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from app.models import ProjectMemoryEvent, ProjectMemoryState
from app.services.normalization import normalize
from app.services.queries import resolve_scope


def mark_dirty(session, project_id, settings, *, origin_key, source_id=None, task_id=None,
               command_source_id=None, event_type="processed_source", immediate=False, now=None):
    if project_id is None or settings.project_memory_enabled is not True:
        return False
    now = now or datetime.now(timezone.utc)
    session.execute(insert(ProjectMemoryState).values(project_id=project_id, is_dirty=False, change_revision=0,
        reconciled_revision=0).on_conflict_do_nothing(index_elements=["project_id"]))
    state = session.scalar(select(ProjectMemoryState).where(ProjectMemoryState.project_id == project_id)
                           .with_for_update().execution_options(populate_existing=True))
    if session.scalar(select(ProjectMemoryEvent.id).where(ProjectMemoryEvent.project_id == project_id,
                                                        ProjectMemoryEvent.origin_key == origin_key)) is not None:
        return False
    if immediate:
        state.retry_after = None
    state.last_change_at = now
    state.change_revision += 1
    state.is_dirty = True
    state.dirty_since = state.dirty_since or now
    state.refresh_after = now if immediate else now + timedelta(seconds=settings.project_memory_debounce_seconds)
    session.add(ProjectMemoryEvent(id=uuid4(), project_id=project_id, revision=state.change_revision,
        origin_key=origin_key, source_id=source_id, task_id=task_id, command_source_id=command_source_id,
        event_type=event_type))
    print("Project memory scheduled: " + str(project_id), flush=True)
    return True


def schedule_refresh(session, name, settings, origin_key=None):
    if settings.project_memory_enabled is not True:
        return "Project Memory esta deshabilitada."
    with session.begin():
        scope = resolve_scope(session, "proyecto " + normalize(name))
        if scope.error:
            return scope.error
        if len(scope.project_ids) != 1:
            return "Indica un proyecto inequivoco."
        mark_dirty(session, scope.project_ids[0], settings,
                   origin_key=origin_key or "manual:" + str(uuid4()), event_type="manual_refresh", immediate=True)
        return scope.label + ": memoria marcada para actualizacion."


def parse_refresh(text):
    parts = text.strip().split(maxsplit=1)
    if not parts or parts[0].casefold().split("@")[0] not in {"/refrescar", "/refresh"}:
        return None
    return parts[1].strip() if len(parts) > 1 else ""
