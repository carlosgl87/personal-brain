from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Project, Source
from app.services.project_matching import resolve_project

TELEGRAM_UNIQUE_PREDICATE = text("external_source = 'telegram' AND external_id IS NOT NULL")


def authorized_envelope(update: dict, user_id: int) -> dict | None:
    if type(update.get("update_id")) is not int or update["update_id"] < 0:
        return None
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    sender = message.get("from")
    chat = message.get("chat")
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    if (type(sender.get("id")) is not int or sender["id"] != user_id
            or sender.get("is_bot") is not False or message.get("sender_chat") is not None):
        return None
    if chat.get("type") != "private" or type(chat.get("id")) is not int or chat["id"] != user_id:
        return None
    if type(message.get("message_id")) is not int or message["message_id"] <= 0:
        return None
    if type(message.get("date")) is not int or message["date"] < 0:
        return None
    if "edited_message" in update and (type(message.get("edit_date")) is not int or message["edit_date"] < 0):
        return None
    return message


def authorized_message(update: dict, user_id: int) -> dict | None:
    message = authorized_envelope(update, user_id)
    if message is None:
        return None
    if not isinstance(message.get("text"), str) or not message["text"].strip():
        return None
    return message


def ingest_update(session: Session, update: dict, user_id: int, is_query=False) -> dict:
    message = authorized_message(update, user_id)
    if message is None:
        return {"status": "ignored"}
    external_id = str(update["update_id"])
    # La transacción se confirma antes de responder o avanzar el offset.
    with session.begin():
        existing = session.scalar(select(Source).where(
            Source.external_source == "telegram", Source.external_id == external_id,
        ))
        if existing is not None:
            return source_result(session, existing, "duplicate")
        project = None if is_query else resolve_project(session, message["text"])
        statement = (
            insert(Source).values(
                source_type="telegram_query" if is_query else "telegram_text", raw_content=message["text"],
                raw_metadata=update, external_source="telegram", external_id=external_id,
                primary_project_id=project.id if project else None,
                processing_status="skipped" if is_query else "pending",
            )
            .on_conflict_do_nothing(
                index_elements=["external_source", "external_id"],
                index_where=TELEGRAM_UNIQUE_PREDICATE,
            ).returning(Source)
        )
        source = session.scalar(statement)
        if source is None:
            source = session.scalar(select(Source).where(
                Source.external_source == "telegram", Source.external_id == external_id,
            ))
            return source_result(session, source, "duplicate")
        return source_result(session, source, "saved", project)


def source_result(session: Session, source: Source, status: str, project=None) -> dict:
    if project is None and source.primary_project_id is not None:
        project = session.get(Project, source.primary_project_id)
    return {
        "status": status,
        "source_id": str(source.id),
        "project_id": str(source.primary_project_id) if source.primary_project_id else None,
        "project_name": project.name if project else None,
    }
