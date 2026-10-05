"""Procesamiento explícito de fuentes pendientes; no imprime textos ni secretos."""
import argparse
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import get_settings
from app.database import get_engine
from app.models import Source
from app.services.processing import SourceNotProcessable, process_source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-id", type=UUID)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--reprocess", action="store_true")
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 100:
        parser.error("--limit debe estar entre 1 y 100.")
    if args.reprocess and args.source_id is None:
        parser.error("--reprocess requiere --source-id para evitar reprocesamiento masivo.")
    try:
        settings = get_settings()
        settings.llm_credentials()
        with Session(get_engine()) as session:
            ids = [args.source_id] if args.source_id else list(session.scalars(
                select(Source.id).where(Source.processing_status.in_(["pending", "failed", "pending_transcription", "transcription_failed", "processing_parts", "parts_complete", "consolidating"]))
                .order_by(Source.received_at, Source.id).limit(args.limit)
            ))
        for source_id in ids:
            with Session(get_engine()) as session:
                result = process_source(session, source_id, settings, force=args.reprocess)
            print(f"Fuente {source_id}: {result['status']}; tareas={result['tasks_count']}, decisiones={result['decisions_count']}")
        print("Procesamiento terminado.")
    except SourceNotProcessable as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("Procesamiento detenido. Revisa configuración, migraciones y disponibilidad de Claude; la fuente original se conserva.") from None


if __name__ == "__main__":
    main()
