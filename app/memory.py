"""Operaciones explícitas; sin backfills en startup ni deployment."""
import argparse
from uuid import UUID
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_engine
from app.services.memory import backfill_candidates, index_source, semantic_search


def main():
    parser = argparse.ArgumentParser(description="Memoria derivada de fuentes")
    commands = parser.add_subparsers(dest="command", required=True)
    backfill = commands.add_parser("backfill")
    backfill.add_argument("--limit", type=int, default=100)
    source = commands.add_parser("source")
    source.add_argument("source_id", type=UUID)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--project")
    search.add_argument("--company")
    search.add_argument("--area")
    search.add_argument("--type", dest="source_type")
    search.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    if args.command == "backfill" and not 1 <= args.limit <= 100:
        parser.error("--limit debe estar entre 1 y 100.")
    try:
        settings = get_settings()
        if args.command == "search":
            with Session(get_engine()) as session:
                results = semantic_search(session, args.query, settings, project=args.project,
                    company=args.company, area=args.area, source_type=args.source_type, limit=args.limit)
            for result in results:
                print("Fuente: " + result["source_id"] + " | Chunk: " + result["chunk_id"] +
                      " | Distancia: " + str(round(result["distance"], 4)))
                print("Proyecto: " + (result["project_id"] or "sin proyecto") +
                      " | Fecha: " + result["received_at"] + " | Tipo: " + result["source_type"] +
                      " | Score: " + str(round(result["score"], 4)))
                print(result["content"])
            print("Resultados: " + str(len(results)))
            return
        if args.command == "source":
            ids = [args.source_id]
        else:
            with Session(get_engine()) as session:
                ids = backfill_candidates(session, settings, args.limit)
        failures = 0
        for source_id in ids:
            try:
                with Session(get_engine()) as session:
                    result = index_source(session, source_id, settings)
                print("Fuente " + str(source_id) + ": embeddings nuevos=" + str(result["embedded"]) +
                      ", pendientes=" + str(result["pending_embeddings"]))
            except Exception:
                failures += 1
                print("Fuente " + str(source_id) + ": memoria pendiente; original y chunks confirmados se conservan.")
        if failures:
            raise SystemExit("Algunas fuentes requieren reintento. Revisa configuración, modelo y migraciones.")
        print("Memoria terminada; fuentes examinadas=" + str(len(ids)))
    except SystemExit:
        raise
    except Exception:
        raise SystemExit("Memoria no completada. Revisa configuración, dimensiones y migraciones; los originales se conservan.") from None


if __name__ == "__main__":
    main()
