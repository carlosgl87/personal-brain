from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.api.routes.telegram import require_telegram_access
from app.config import Settings, get_settings
from app.database import get_session
from app.services.claude import ExtractionError
from app.services.transcription import TranscriptionError
from app.services.memory import maybe_index
from app.services.processing import SourceNotFound, SourceNotProcessable, process_source

router = APIRouter(prefix="/sources", tags=["processing"])


@router.post("/{source_id}/process", dependencies=[Depends(require_telegram_access)])
def process(
    source_id: UUID, force: bool = Query(False),
    action_plan: bool = Query(False),
    settings: Settings = Depends(get_settings), session: Session = Depends(get_session),
):
    try:
        result = process_source(session, source_id, settings, force=force,
                                **({"action_plan": True} if action_plan else {}))
        if result.get("action_plan"):
            from app.services.message_handling import respond_to_plan
            result["answer"] = respond_to_plan(session, result, settings)
        if not (result.get("action_plan") and result.get("interaction") == "query"):
            maybe_index(session, UUID(result.get("transcript_source_id", str(source_id))), settings)
        return result
    except SourceNotFound:
        raise HTTPException(status_code=404, detail="Fuente no encontrada.") from None
    except SourceNotProcessable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except TranscriptionError:
        raise HTTPException(status_code=502, detail="No se completó la transcripción. El audio original se conserva.") from None
    except ExtractionError:
        raise HTTPException(status_code=502, detail="Falló el procesamiento. La fuente original se conserva.") from None
    except ValueError:
        raise HTTPException(status_code=503, detail="Configura LLM_PROVIDER, LLM_MODEL y LLM_API_KEY.") from None
