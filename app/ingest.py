import argparse
from sqlalchemy.orm import Session
from app.config import get_settings
from app.database import get_engine
from app.services.file_ingestion import FILE_TYPES, ingest_file
from app.services.memory import index_source
from app.services.processing import SourceNotProcessable, process_source


def main():
    parser = argparse.ArgumentParser(description="Guardar una transcripción completa y generar memoria")
    parser.add_argument("--file", required=True)
    parser.add_argument("--type", dest="source_type", choices=FILE_TYPES, default="meeting_transcript")
    parser.add_argument("--project")
    parser.add_argument("--process", action="store_true")
    args = parser.parse_args()
    try:
        settings = get_settings()
        with Session(get_engine()) as session:
            source_id = ingest_file(session, args.file, args.source_type, args.project)
        print("Fuente completa guardada: " + str(source_id))
        failed = False
        try:
            with Session(get_engine()) as session:
                result = index_source(session, source_id, settings)
            print("Memoria: embeddings nuevos=" + str(result["embedded"]) + ", pendientes=" + str(result["pending_embeddings"]))
        except Exception:
            failed = True
            print("Memoria pendiente; original guardado. Reintenta con python -m app.memory source " + str(source_id))
        if args.process:
            try:
                with Session(get_engine()) as session:
                    process_source(session, source_id, settings)
                print("Extracción terminada.")
            except SourceNotProcessable as exc:
                failed = True
                print(str(exc))
            except Exception:
                failed = True
                print("Extracción pendiente; el original se conserva.")
        if failed:
            raise SystemExit("Ingesta guardada con procesamiento pendiente.")
    except SystemExit:
        raise
    except Exception:
        raise SystemExit("No se completó la ingesta. Revisa formato UTF-8, configuración y migraciones; no se borró información.") from None


if __name__ == "__main__":
    main()
