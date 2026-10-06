"""Preserve manual task changes and invalidate every affected project on an edit."""
from sqlalchemy import select, or_, and_, BigInteger, cast
from app.models import Source, Task, TaskChange
from app.services.queries import chat_identity, message_identity, revision_time, current_source, current_derived


def predecessor_statement(source, current_only=True):
    message = source.raw_metadata.get("edited_message")
    if not isinstance(message, dict) or source.source_type not in {"telegram_text", "audio_transcript"}:
        return None
    timestamp = message.get("edit_date", message.get("date"))
    statement = select(Source).where(
        Source.id != source.id, Source.source_type.in_(["telegram_text", "audio_transcript"]),
        Source.latest_processing_run_id.is_not(None),
        chat_identity(Source) == str(message["chat"]["id"]),
        message_identity(Source, "message_id") == str(message["message_id"]),
        or_(revision_time(Source) < timestamp, and_(revision_time(Source) == timestamp,
            cast(Source.raw_metadata["update_id"].astext, BigInteger) < source.raw_metadata["update_id"])))
    return statement.where(current_source()) if current_only else statement


def revision_projects(session, source):
    statement = predecessor_statement(source)
    if statement is None:
        return set()
    previous = session.scalars(statement).all()
    affected = {row.primary_project_id for row in previous if row.primary_project_id}
    ids = [row.id for row in previous]
    if not ids:
        return affected
    # Share the task locks used by manual editing; checked again during final publication.
    tasks = session.scalars(select(Task).join(Source, Source.id == Task.source_id).where(
        Task.source_id.in_(ids), current_derived(Task)).with_for_update(of=Task)).all()
    if tasks and session.scalar(select(TaskChange.id).where(TaskChange.task_id.in_([row.id for row in tasks])).limit(1)):
        from app.services.processing import SourceNotProcessable
        raise SourceNotProcessable("La nota anterior tiene tareas editadas manualmente; conserva esos cambios y guarda la correccion como una nota nueva.")
    affected.update(row.project_id for row in tasks if row.project_id)
    return affected


def revision_delta(session, event):
    source = session.get(Source, event.source_id)
    statement = predecessor_statement(source, current_only=False) if source else None
    ids=session.scalars(statement.where(Source.primary_project_id == event.project_id).with_only_columns(Source.id)).all() if statement is not None else []
    return {"source_id":str(event.source_id), "project_id":str(event.project_id),
        "event_type":"source_revision", "superseded_source_ids":[str(identifier) for identifier in ids],
        "current_source_project_id":str(source.primary_project_id) if source and source.primary_project_id else None,
        "note":"La nueva edicion reemplaza las fuentes indicadas. Sus tareas y decisiones anteriores ya no estan vigentes; prioriza la nueva asociacion de proyecto y el estado SQL actual."}
