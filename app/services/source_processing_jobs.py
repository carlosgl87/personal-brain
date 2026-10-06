"""Persistent queue ownership, backoff and compact read-only status."""
import hashlib
from datetime import timedelta
from uuid import UUID, uuid4
from sqlalchemy import and_, or_, select
from app.models import DocumentAsset, SourceProcessingJob
from app.services.queries import brief, date_label

MAX_ATTEMPTS = 6
BACKOFF_SECONDS = (60, 300, 900, 3600)


def lock_id(source_id):
    return int.from_bytes(hashlib.sha256(('source-processing:' + str(source_id)).encode()).digest()[:8], 'big', signed=True)


def ready(now):
    return or_(SourceProcessingJob.status == 'processing', and_(
        SourceProcessingJob.status.in_(['pending', 'retry']), SourceProcessingJob.next_retry_at <= now))


def candidate_ids(session, now):
    return list(session.scalars(select(SourceProcessingJob.source_id).where(ready(now)).order_by(
        SourceProcessingJob.next_retry_at, SourceProcessingJob.created_at, SourceProcessingJob.id).limit(100)))


def claim_job(session, source_id, now):
    # A dedicated session advisory lock must already be held for this source.
    job = session.scalar(select(SourceProcessingJob).where(SourceProcessingJob.source_id == source_id,
        ready(now)).with_for_update(skip_locked=True))
    if job is None:
        return None
    if job.attempts >= MAX_ATTEMPTS:
        job.status, job.last_error_code, job.claim_token = 'failed', 'attempts_exhausted', None
        return None
    job.status, job.claimed_at, job.claim_token = 'processing', now, uuid4()
    job.attempts += 1
    return job.claim_token


def finish_job(session, source_id, token, now, error_code=None):
    job = session.scalar(select(SourceProcessingJob).where(SourceProcessingJob.source_id == source_id,
        SourceProcessingJob.status == 'processing', SourceProcessingJob.claim_token == token).with_for_update())
    if job is None:
        return False
    job.claim_token = None
    job.last_error_code = error_code
    if error_code is None:
        job.status, job.completed_at = 'completed', now
    else:
        job.status = 'failed' if job.attempts >= MAX_ATTEMPTS else 'retry'
        job.next_retry_at = now + timedelta(seconds=BACKOFF_SECONDS[min(job.attempts - 1, len(BACKOFF_SECONDS)-1)])
    return True


def processing_argument(text):
    raw = text.strip().split(maxsplit=1)
    if not raw or raw[0].casefold().split('@')[0] != '/procesamiento':
        return None
    return raw[1].strip() if len(raw) > 1 else ''


def job_line(job, filename):
    value = f"• {brief(filename or 'Fuente', 100)} — {job.status} | Intentos: {job.attempts}\n  Fuente: {job.source_id}"
    if job.status == 'retry':
        value += '\n  Reintento: ' + date_label(job.next_retry_at)
    if job.last_error_code:
        value += '\n  Código: ' + job.last_error_code
    return value


def answer_processing(session, argument):
    statement = select(SourceProcessingJob, DocumentAsset.filename).outerjoin(
        DocumentAsset, DocumentAsset.source_id == SourceProcessingJob.source_id)
    with session.begin():
        if argument:
            try:
                source_id = UUID(argument)
            except ValueError:
                return 'Usa /procesamiento o /procesamiento UUID de fuente.'
            row = session.execute(statement.where(SourceProcessingJob.source_id == source_id)).first()
            return job_line(*row) if row else 'No encontré un trabajo para esa fuente.'
        lines = []
        for label, status in [('Procesando', 'processing'), ('Pendientes', 'pending'), ('En reintento', 'retry'),
                              ('Completados recientes', 'completed'), ('Fallidos recientes', 'failed')]:
            order = SourceProcessingJob.completed_at.desc().nulls_last() if status == 'completed' else SourceProcessingJob.created_at.desc()
            rows = session.execute(statement.where(SourceProcessingJob.status == status).order_by(order,
                SourceProcessingJob.id).limit(3)).all()
            lines.append(label + ':')
            lines.extend(job_line(*row) for row in rows)
            if not rows:
                lines.append('Sin trabajos.')
        return '\n'.join(lines)
