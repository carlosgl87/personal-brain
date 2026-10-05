"""Captura del archivo original antes de transcribir; errores sin URLs ni tokens."""
import hashlib
import logging
import re
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models import AudioAsset, Source
from app.services.telegram_ingestion import (
    TELEGRAM_UNIQUE_PREDICATE, authorized_envelope, source_result,
)


class AudioError(RuntimeError):
    pass


class AudioTooLarge(AudioError):
    pass


def authorized_audio(update, user_id):
    message = authorized_envelope(update, user_id)
    if message is None:
        return None
    media = message.get("voice") or message.get("audio")
    if not isinstance(media, dict):
        return None
    if not all(isinstance(media.get(key), str) and media[key] for key in ("file_id", "file_unique_id")):
        return None
    if type(media.get("duration")) is not int or media["duration"] < 0:
        return None
    if "file_size" in media and (type(media["file_size"]) is not int or media["file_size"] < 1):
        return None
    if "caption" in message and not isinstance(message["caption"], str):
        return None
    if "mime_type" in media and (not isinstance(media["mime_type"], str) or len(media["mime_type"]) > 100):
        return None
    return message


def download_audio(token, media, max_bytes, client=None):
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
        logging.getLogger(name).propagate = False

    def download(http):
        try:
            response = http.post("https://api.telegram.org/bot" + token + "/getFile",
                                 json={"file_id": media["file_id"]})
            response.raise_for_status()
            result = response.json()
            if result.get("ok") is not True:
                raise AudioError("Telegram no permitió descargar el archivo.")
            info = result["result"]
            path = info.get("file_path", "")
            # Solo rutas relativas de Telegram; sin query, segmentos .. ni redirecciones.
            if not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or any(p in {"", ".", ".."} for p in path.split("/")):
                raise AudioError("La referencia de audio no es válida.")
            if info.get("file_size", 0) > max_bytes:
                raise AudioTooLarge("El archivo supera el límite permitido.")
            url = "https://api.telegram.org/file/bot" + token + "/" + path
            data = bytearray()
            with http.stream("GET", url) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > max_bytes:
                        raise AudioTooLarge("El archivo supera el límite permitido.")
                    data.extend(chunk)
            expected_size = media.get("file_size", info.get("file_size"))
            if not data or (expected_size is not None and len(data) != expected_size):
                raise AudioError("El archivo descargado está incompleto.")
            return bytes(data)
        except AudioError:
            raise
        except Exception:
            raise AudioError("No se pudo descargar el audio; la referencia se conserva para reintentar.") from None
    if client is not None:
        return download(client)
    with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as http:
        return download(http)


def ingest_audio(session, update, user_id, settings):
    message = authorized_audio(update, user_id)
    if message is None:
        return {"status": "ignored"}
    media = message.get("voice") or message["audio"]
    max_bytes = settings.audio_max_bytes
    token, _ = settings.telegram_credentials()
    external_id = str(update["update_id"])
    with session.begin():
        source = session.scalar(select(Source).where(
            Source.external_source == "telegram", Source.external_id == external_id,
        ))
        created = source is None
        if created:
            source = session.scalar(insert(Source).values(
                id=uuid4(), source_type="telegram_audio",
                raw_content=message.get("caption") or "", raw_metadata=update,
                external_source="telegram", external_id=external_id,
                processing_status="awaiting_download",
            ).on_conflict_do_nothing(
                index_elements=["external_source", "external_id"],
                index_where=TELEGRAM_UNIQUE_PREDICATE,
            ).returning(Source))
            if source is None:
                created = False
                source = session.scalar(select(Source).where(
                    Source.external_source == "telegram", Source.external_id == external_id,
                ))
        source_id = source.id

    # Serializa las descargas repetidas de una misma fuente.
    with session.begin():
        source = session.scalar(select(Source).where(Source.id == source_id).with_for_update())
        if source.source_type != "telegram_audio":
            raise AudioError("El update existente no corresponde a un audio.")
        asset = session.scalar(select(AudioAsset).where(AudioAsset.source_id == source_id))
        downloaded = asset is None
        if downloaded:
            try:
                if media["duration"] > settings.audio_max_seconds or media.get("file_size", 0) > max_bytes:
                    raise AudioTooLarge("El audio supera los límites configurados.")
                data = download_audio(token, media, max_bytes)
            except AudioTooLarge:
                source.processing_status = "audio_rejected"
                return {"status": "answered", "source_id": str(source.id),
                        "answer": f"El audio supera el límite de {max_bytes // (1024 * 1024)} MB o {settings.audio_max_seconds} segundos. Conservé su referencia, pero no el archivo. Envía un audio más corto."}
            session.add(AudioAsset(
                source_id=source_id, content=data, sha256=hashlib.sha256(data).hexdigest(),
                mime_type=media.get("mime_type") or ("audio/ogg" if message.get("voice") else "application/octet-stream"),
            ))
            source.processing_status = "pending_transcription"
        result = source_result(session, source, "saved" if created or downloaded else "duplicate")
        # Un primer intento pudo fallar después de conservar la referencia.
        result["media_type"] = "audio"
        return result
