"""Durable document processing. Original data and successful stages survive retries."""
import argparse
import logging
import signal
import threading
from datetime import datetime, timezone
from uuid import UUID
import httpx
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.config import get_settings
from app.database import get_engine
from app.models import DocumentAsset, Project, Source, SourceProcessingJob
from app.services.chunking import chunk_source
from app.services.memory import index_source
from app.services.processing import process_source
from app.services.source_processing_jobs import candidate_ids, claim_job, finish_job, lock_id
from app.telegram import TelegramAPI


def notify_completed(engine, source_id, result, settings):
    try:
        token, user_id = settings.telegram_credentials()
        with Session(engine) as session, session.begin():
            asset = session.scalar(select(DocumentAsset).where(DocumentAsset.source_id == source_id))
            source = session.get(Source, source_id)
            if asset is None or source is None or source.external_source != 'telegram':
                return
            project = session.get(Project, source.primary_project_id) if source.primary_project_id else None
            message = (f"Procesamiento completado: {asset.filename}\nProyecto: {project.name if project else 'sin proyecto identificado'}"
                f"\nTareas: {result['tasks_count']}\nDecisiones: {result['decisions_count']}\nFuente: {source_id}")
        if result.get('action_plan'):
            from app.services.message_handling import respond_to_plan
            with Session(engine) as session:
                message += '\n' + respond_to_plan(session, result, settings)
        for name in ('httpx', 'httpcore'):
            logging.getLogger(name).setLevel(logging.CRITICAL)
            logging.getLogger(name).propagate = False
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
            TelegramAPI(client, token).call('sendMessage', {'chat_id': user_id, 'text': message})
    except Exception:
        print('Document completed; notification unavailable: ' + str(source_id), flush=True)


class ExtractionLimitError(ValueError):
    pass


def preflight(session, source_id, settings):
    with session.begin():
        source = session.get(Source, source_id)
        if source is None:
            raise ValueError('missing_source')
        if len(source.raw_content) > settings.extraction_max_chars:
            if not settings.hierarchical_extraction_enabled or len(chunk_source(source, settings)) > settings.hierarchical_max_chunks:
                raise ExtractionLimitError('extraction_limit')


def process_job(engine, source_id, settings, clock):
    connection = None
    acquired = False
    try:
        # Session lock survives stage commits. All stage transactions use this same
        # connection with normal isolation (never AUTOCOMMIT for ORM writes).
        connection = engine.connect()
        acquired = bool(connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': lock_id(source_id)}))
        connection.commit()
        if not acquired:
            return 0, None
        with Session(connection) as session, session.begin():
            claim_token = claim_job(session, source_id, clock())
        if claim_token is None:
            return 0, None
        error = None
        result = None
        try:
            with Session(connection) as session:
                preflight(session, source_id, settings)
                index_source(session, source_id, settings)
                result = process_source(session, source_id, settings)
                if result.get('status') not in {'processed', 'already_processed'}:
                    raise ValueError('processing_result')
        except ExtractionLimitError:
            error = 'extraction_limit'
        except Exception:
            error = 'processing_failed'
        connection.scalar(text('SELECT 1'))
        connection.commit()
        with Session(connection) as session, session.begin():
            published = finish_job(session, source_id, claim_token, clock(), error)
        return 1, result if published and error is None else None
    except Exception:
        print('Source worker retry pending: ' + str(source_id), flush=True)
        return 0, None
    finally:
        if connection is not None:
            try:
                connection.rollback()
                if acquired:
                    connection.scalar(text('SELECT pg_advisory_unlock(:key)'), {'key': lock_id(source_id)})
                    connection.commit()
            except Exception:
                connection.invalidate()
            connection.close()


def run_once(engine, settings, now=None):
    if settings.source_processing_worker_enabled is not True:
        return 0
    clock = lambda: now or datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        ids = candidate_ids(session, clock())
    for source_id in ids:
        handled, result = process_job(engine, source_id, settings, clock)
        if handled:
            if result is not None:
                # Queue commit and lock release precede the best-effort notification.
                notify_completed(engine, source_id, result, settings)
            return 1
    return 0


def retry_source(engine, source_id):
    with engine.connect() as connection:
        key = lock_id(source_id)
        acquired = bool(connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}))
        connection.commit()
        if not acquired:
            return False
        try:
            with Session(connection) as session, session.begin():
                job = session.scalar(select(SourceProcessingJob).where(SourceProcessingJob.source_id == source_id).with_for_update())
                if job is None or job.status not in {'failed', 'retry', 'processing'}:
                    return False
                job.status, job.attempts, job.claim_token = 'pending', 0, None
                job.next_retry_at, job.last_error_code = datetime.now(timezone.utc), None
                job.claimed_at, job.completed_at = None, None
                return True
        finally:
            try:
                connection.rollback()
                connection.scalar(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
                connection.commit()
            except Exception:
                connection.invalidate()


def run(stop, settings, engine):
    while not stop.is_set():
        try:
            processed = run_once(engine, settings)
        except Exception:
            processed = 0
            print('Source processing worker retry pending; sensitive details omitted.', flush=True)
        stop.wait(1 if processed else 5)


def main():
    parser = argparse.ArgumentParser(description='Cola persistente de documentos')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--retry', type=UUID, metavar='SOURCE_UUID')
    args = parser.parse_args()
    try:
        settings = get_settings()
        if args.retry:
            print('Trabajo encolado.' if retry_source(get_engine(), args.retry) else 'Trabajo no disponible para reintento.')
            return
        if settings.source_processing_worker_enabled is not True:
            return
        if args.once:
            print('Trabajos atendidos: ' + str(run_once(get_engine(), settings)))
            return
        stop = threading.Event()
        for name in ('SIGTERM', 'SIGINT'):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
        run(stop, settings, get_engine())
    except Exception:
        raise SystemExit('Worker detenido. Revisa configuración, conexión y migraciones; detalles omitidos.') from None


if __name__ == '__main__':
    main()
