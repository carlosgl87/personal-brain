"""Telegram TXT/MD: originals + queue in one transaction, no model calls."""
import hashlib
import unicodedata
from pathlib import PurePosixPath
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import selectinload
from app.models import Area, Company, DocumentAsset, Project, Source, SourceProcessingJob
from app.services.audio import AudioError, AudioTooLarge, download_audio
from app.services.normalization import normalize
from app.services.project_matching import resolve_document_project
from app.services.telegram_ingestion import TELEGRAM_UNIQUE_PREDICATE, authorized_envelope, source_result


class DocumentError(ValueError):
    pass


def authorized_document(update, user_id):
    message = authorized_envelope(update, user_id)
    return message if message is not None and isinstance(message.get("document"), dict) else None


def validate_document(message, max_bytes):
    media = message["document"]
    filename = media.get("file_name")
    if (not isinstance(filename, str) or not filename.strip() or len(filename) > 255
            or filename != filename.strip() or filename in {".", ".."}
            or any(char in filename for char in '/\\:<>"|?*')
            or any(unicodedata.category(char) in {'Cc', 'Cf'} for char in filename)
            or filename.endswith((".", " "))):
        raise DocumentError("El nombre del archivo no es válido. Envía un nombre sin rutas.")
    if PurePosixPath(filename).suffix.casefold() not in {".txt", ".md"}:
        raise DocumentError("Solo se admiten archivos .txt y .md codificados en UTF-8.")
    for key in ("file_id", "file_unique_id"):
        value = media.get(key)
        if key == "file_unique_id" and value is None:
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > 1024:
            raise DocumentError("La referencia del documento no es válida.")
    size = media.get("file_size")
    if "file_size" in media:
        if type(size) is not int or size <= 0:
            raise DocumentError("El tamaño del documento no es válido.")
        if size > max_bytes:
            raise DocumentError("El archivo supera el límite permitido de documentos.")
    mime = media.get("mime_type")
    if mime is not None and (not isinstance(mime, str) or len(mime) > 100):
        raise DocumentError("Los metadatos del documento no son válidos.")
    if "caption" in message and not isinstance(message["caption"], str):
        raise DocumentError("El caption del documento no es válido.")
    return filename


def decode_document(data):
    try:
        content = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise DocumentError("El archivo no está codificado en UTF-8 y no pudo procesarse.") from None
    if not content.strip() or "\x00" in content:
        raise DocumentError("El archivo está vacío o contiene datos binarios.")
    return content


def document_scope(session, content, caption, filename=""):
    source_type = "document_text"
    target = caption.strip()
    if target.startswith("/"):
        parts = target.split(maxsplit=1)
        command = parts[0].casefold().split("@")[0]
        if command not in {"/proyecto", "/reunion", "/documento"}:
            raise DocumentError("Usa como caption SIMA, /proyecto SIMA, /reunion SIMA o /documento SIMA.")
        source_type = "meeting_transcript" if command == "/reunion" else "document_text"
        target = parts[1].strip() if len(parts) > 1 else ""
        if not target:
            raise DocumentError("Indica un proyecto en el caption o envía el archivo sin caption.")
    projects = session.scalars(select(Project).where(Project.status == "active", Project.archived_at.is_(None))
                               .options(selectinload(Project.aliases))).all()
    if not target:
        companies = session.scalars(select(Company)).all()
        areas = session.scalars(select(Area)).all()
        return source_type, resolve_document_project(filename, content, projects, companies, areas), None
    key = normalize(target)
    matches = [p for p in projects if key in {normalize(n) for n in (p.name, p.slug, *(a.alias for a in p.aliases))}]
    if len(matches) == 1:
        return source_type, matches[0], None
    warning = ("Archivo guardado, pero no encontré ese proyecto. Quedó sin proyecto identificado."
               if not matches else "Archivo guardado, pero el proyecto indicado es ambiguo. Quedó sin proyecto identificado.")
    return source_type, None, warning


def document_receipt(session, source, status, warning=None):
    asset = session.scalar(select(DocumentAsset).where(DocumentAsset.source_id == source.id))
    job = session.scalar(select(SourceProcessingJob).where(SourceProcessingJob.source_id == source.id))
    if asset is None or job is None:
        raise DocumentError("El update existente no corresponde a un documento encolado.")
    result = source_result(session, source, status)
    label = result.get("project_name") or "sin proyecto identificado"
    state = "encolado" if job.status == "pending" else job.status
    result.update(media_type="document", source_type=source.source_type, filename=asset.filename,
                  processing_status=job.status,
                  answer=f"Archivo guardado: {asset.filename}\nProyecto: {label}\nTipo: {source.source_type}\nProcesamiento: {state}\nFuente: {source.id}")
    if warning:
        result["answer"] += "\n" + warning
    return result


def ingest_document(session, update, user_id, settings):
    message = authorized_document(update, user_id)
    if message is None:
        return {"status": "ignored"}
    filename = validate_document(message, settings.telegram_document_max_bytes)
    external_id = str(update["update_id"])
    predicate = (Source.external_source == "telegram", Source.external_id == external_id)
    with session.begin():
        existing = session.scalar(select(Source).where(*predicate))
        if existing is not None:
            return document_receipt(session, existing, "duplicate")
    token, _ = settings.telegram_credentials()
    try:
        data = download_audio(token, message["document"], settings.telegram_document_max_bytes)
    except AudioTooLarge:
        raise DocumentError("El archivo supera el límite permitido de documentos.") from None
    except AudioError:
        # API maps this to a retryable HTTP error, without exposing the Telegram URL.
        raise AudioError("No se pudo descargar el documento; se puede reintentar.") from None
    content = decode_document(data)
    with session.begin():
        kind, project, warning = document_scope(session, content, message.get("caption", ""), filename)
        source = session.scalar(insert(Source).values(
            id=uuid4(), source_type=kind, raw_content=content, raw_metadata=update | {"processing_schema": "action-plan-v1"},
            primary_project_id=project.id if project else None, external_source="telegram",
            external_id=external_id, processing_status="pending",
        ).on_conflict_do_nothing(index_elements=["external_source", "external_id"],
                                index_where=TELEGRAM_UNIQUE_PREDICATE).returning(Source))
        if source is None:
            return document_receipt(session, session.scalar(select(Source).where(*predicate)), "duplicate")
        media = message["document"]
        session.add(DocumentAsset(source_id=source.id, filename=filename, mime_type=media.get("mime_type"),
            file_size=len(data), sha256=hashlib.sha256(data).hexdigest(), original_bytes=data,
            telegram_file_id=media["file_id"], telegram_file_unique_id=media.get("file_unique_id")))
        session.add(SourceProcessingJob(source_id=source.id))
        session.flush()
        return document_receipt(session, source, "saved", warning)
