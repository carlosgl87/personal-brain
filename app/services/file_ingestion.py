"""Ingesta UTF-8 completa; no persiste rutas absolutas."""
import hashlib
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone
from sqlalchemy import select, text

from app.models import Source
from app.services.normalization import normalize
from app.services.queries import resolve_scope

FILE_TYPES = ("meeting_transcript", "manual_note", "document_text")


def read_text_file(filename):
    path = Path(filename)
    if path.suffix.lower() not in {".txt", ".md"}:
        raise ValueError("Solo se admiten .txt y .md.")
    with path.open("rb") as handle:
        raw = handle.read(10 * 1024 * 1024 + 1)
    if not raw or len(raw) > 10 * 1024 * 1024 or b"\x00" in raw:
        raise ValueError("Archivo vacío, binario o mayor de 10 MiB.")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("El archivo debe estar codificado en UTF-8.") from None
    if not content.strip():
        raise ValueError("Texto vacío.")
    return content, {"filename": path.name, "sha256": hashlib.sha256(raw).hexdigest(),
                     "file_size": len(raw), "ingest_method": "text-file-cli"}


def ingest_file(session, filename, source_type="meeting_transcript", project_name=None):
    if source_type not in FILE_TYPES:
        raise ValueError("Tipo de fuente no admitido.")
    content, metadata = read_text_file(filename)
    with session.begin():
        project_id = None
        if project_name:
            scope = resolve_scope(session, "proyecto " + normalize(project_name))
            if not scope.error and len(scope.project_ids) == 1:
                project_id = scope.project_ids[0]
        external_id = hashlib.sha256((metadata["sha256"] + "|" + source_type + "|" + str(project_id)).encode()).hexdigest()
        lock = int.from_bytes(bytes.fromhex(external_id[:16]), "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
        existing = session.scalar(select(Source).where(Source.external_source == "text-file-cli", Source.external_id == external_id))
        if existing:
            return existing.id
        source_id = uuid4()
        session.add(Source(id=source_id, source_type=source_type, raw_content=content,
            raw_metadata=metadata | {"processing_schema": "action-plan-v1"}, primary_project_id=project_id, external_source="text-file-cli",
            external_id=external_id, processing_status="pending", received_at=datetime.now(timezone.utc)))
        return source_id
