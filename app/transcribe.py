"""Transcribe un audio guardado con OpenRouter sin llamar a Claude."""
import argparse
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_engine
from app.services.transcription import TranscriptionError, transcribe_audio


def main():
    parser = argparse.ArgumentParser(description="Transcribir un audio original con OpenRouter")
    parser.add_argument("--source-id", type=UUID, required=True)
    args = parser.parse_args()
    try:
        with Session(get_engine()) as session:
            transcript_id = transcribe_audio(session, args.source_id, get_settings())
        print("Transcripción guardada. Fuente: " + str(transcript_id))
    except TranscriptionError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("No se completó la transcripción. Revisa configuración y migraciones; el original se conserva.") from None


if __name__ == "__main__":
    main()
