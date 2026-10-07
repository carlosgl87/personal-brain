import hmac
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Source

from app.config import Settings, get_settings
from app.database import get_session
from app.services.telegram_ingestion import authorized_message, ingest_update
from app.services.queries import answer_query, parse_query
from app.services.task_management import edit_task, parse_task_command
from app.services.task_completion import complete_from_note, has_completion_signal
from app.services.audio import AudioError, authorized_audio, ingest_audio
from app.services.reasoning import answer_reasoning, reasoning_question
from app.services.memory import maybe_index
from app.services.documents import DocumentError, authorized_document, ingest_document
from app.services.intent_router import AMBIGUOUS_REPLY, ERROR_REPLY, deterministic_intent, route_intent
from app.services.source_processing_jobs import answer_processing, processing_argument
from app.services.queries import catalog_query
from app.services.project_memory_events import parse_refresh, schedule_refresh

router = APIRouter(prefix="/telegram", tags=["telegram"])
bearer = HTTPBearer(auto_error=False)


def require_telegram_access(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
) -> int:
    try:
        token, user_id = settings.telegram_credentials()
    except ValueError:
        raise HTTPException(status_code=503, detail="Telegram no está configurado.") from None
    if (credentials is None or credentials.scheme.casefold() != "bearer"
            or not hmac.compare_digest(credentials.credentials.encode(), token.encode())):
        raise HTTPException(status_code=401, detail="No autorizado.")
    return user_id


@router.post("/updates")
def receive_update(
    update: dict, user_id: int = Depends(require_telegram_access),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    if authorized_document(update, user_id) is not None:
        try:
            return ingest_document(session, update, user_id, settings)
        except DocumentError as exc:
            return {"status": "answered", "answer": str(exc)}
        except AudioError:
            raise HTTPException(status_code=502, detail="No se completó la descarga del documento; se puede reintentar.") from None
    if authorized_audio(update, user_id) is not None:
        try:
            return ingest_audio(session, update, user_id, settings)
        except AudioError:
            raise HTTPException(status_code=502, detail="No se completó la descarga del audio; se puede reintentar.") from None
    message = authorized_message(update, user_id)
    if message is None:
        return {"status": "ignored"}
    argument = processing_argument(message["text"])
    if argument is not None:
        saved = ingest_update(session, update, user_id, is_query=True)
        return {"status": "answered", "source_id": saved["source_id"], "answer": answer_processing(session, argument)}
    refresh = parse_refresh(message["text"]) if message else None
    if refresh is not None:
        saved = ingest_update(session, update, user_id, is_query=True)
        answer = schedule_refresh(session, refresh, settings, origin_key="telegram:" + saved["source_id"]) if refresh else "Usa /refrescar NombreProyecto."
        return {"status": "answered", "source_id": saved["source_id"], "answer": answer}
    command = parse_task_command(message["text"]) if message else None
    if command is not None:
        saved = ingest_update(session, update, user_id, is_query=True)
        return {"status": "answered", "source_id": saved["source_id"],
                "answer": edit_task(session, command, saved["source_id"], settings=settings)}
    raw = message["text"]
    # All explicit commands keep priority. /nota is an unconditional override.
    name = raw.strip().split(maxsplit=1)[0].casefold().split("@")[0]
    explicit_question = reasoning_question(raw) if name in {"/ask", "/pregunta"} else None
    if raw.strip().startswith("/") and name not in {"/ask", "/pregunta", "/nota"}:
        saved = ingest_update(session, update, user_id, is_query=True)
        return {"status": "answered", "source_id": saved["source_id"], "answer": answer_query(session, parse_query(raw))}
    if deterministic_intent(raw) is None:
        # Avoid paying classification again when replaying a previously saved update.
        with session.begin():
            existing = session.scalar(select(Source.source_type).where(Source.external_source == "telegram",
                Source.external_id == str(update["update_id"])))
        intent = {"telegram_query": "query", "telegram_text": "new_information"}.get(existing) if isinstance(existing, str) else None
    else:
        intent = None
    try:
        intent = intent or route_intent(raw, settings)
    except Exception:
        return {"status": "answered", "answer": ERROR_REPLY}
    if intent == "ambiguous":
        return {"status": "answered", "answer": AMBIGUOUS_REPLY}
    if intent == "new_information":
        saved = ingest_update(session, update, user_id)
        if saved["status"] in {"saved", "duplicate"}:
            if has_completion_signal(raw):
                completion_answer = complete_from_note(session, UUID(saved["source_id"]), settings)
                if completion_answer:
                    saved["task_completion_answer"] = completion_answer
            maybe_index(session, UUID(saved["source_id"]), settings)
        return saved
    saved = ingest_update(session, update, user_id, is_query=True)
    question = explicit_question if explicit_question is not None else raw.strip()
    simple = catalog_query(question)
    answer = answer_query(session, simple) if simple else answer_reasoning(session, question, saved["source_id"], settings)
    return {"status": "answered", "source_id": saved["source_id"], "answer": answer}
