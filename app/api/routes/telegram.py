import hmac
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_session
from app.services.telegram_ingestion import authorized_message, ingest_update
from app.services.queries import answer_query, parse_query
from app.services.task_management import edit_task, parse_task_command
from app.services.audio import AudioError, authorized_audio, ingest_audio
from app.services.reasoning import answer_reasoning, reasoning_question
from app.services.memory import maybe_index

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
    if authorized_audio(update, user_id) is not None:
        try:
            return ingest_audio(session, update, user_id, settings)
        except AudioError:
            raise HTTPException(status_code=502, detail="No se completó la descarga del audio; se puede reintentar.") from None
    message = authorized_message(update, user_id)
    command = parse_task_command(message["text"]) if message else None
    if command is not None:
        saved = ingest_update(session, update, user_id, is_query=True)
        return {"status": "answered", "source_id": saved["source_id"],
                "answer": edit_task(session, command, saved["source_id"])}
    question = reasoning_question(message["text"]) if message else None
    if question is not None:
        saved = ingest_update(session, update, user_id, is_query=True)
        return {"status": "answered", "source_id": saved["source_id"],
                "answer": answer_reasoning(session, question, saved["source_id"], settings)}
    query = parse_query(message["text"]) if message and message["text"].strip().startswith("/") else None
    if query is None:
        saved = ingest_update(session, update, user_id)
        if saved["status"] in {"saved", "duplicate"}:
            maybe_index(session, UUID(saved["source_id"]), settings)
        return saved
    saved = ingest_update(session, update, user_id, is_query=True)
    return {"status": "answered", "source_id": saved["source_id"],
            "answer": answer_query(session, query)}
