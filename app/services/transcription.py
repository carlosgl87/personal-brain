"""Transcripción remota; no carga modelos locales ni registra audio o credenciales."""
import base64
import hashlib
import logging
import math
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.models import AudioAsset, Source
from app.services.project_matching import resolve_project


class TranscriptionError(RuntimeError):
    pass


def audio_format(data, mime_type):
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"fLaC"):
        return "flac"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "m4a"
    formats = {
        "audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3",
        "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
        "audio/flac": "flac", "audio/x-flac": "flac", "audio/mp4": "m4a",
        "audio/x-m4a": "m4a", "audio/webm": "webm", "audio/aac": "aac",
    }
    result = formats.get((mime_type or "").split(";")[0].strip().lower())
    if result is None:
        raise TranscriptionError("Formato de audio no compatible; el original se conserva.")
    return result


def transcribe_bytes(data, settings, vocabulary=(), *, mime_type="audio/ogg", usage=None, client=None):
    try:
        key = settings.openrouter_api_key
        if key is None or not key.get_secret_value().strip():
            raise TranscriptionError("Configura OPENROUTER_API_KEY; el audio original se conserva.")
        if not data or len(data) > settings.audio_max_bytes:
            raise TranscriptionError("Tamaño de audio no válido; el original se conserva.")
        payload = {
            "model": settings.openrouter_stt_model,
            "input_audio": {"data": base64.b64encode(data).decode("ascii"),
                            "format": audio_format(data, mime_type)},
            "language": settings.stt_language,
            "response_format": "json",
            "temperature": 0,
        }
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
            logging.getLogger(name).propagate = False

        def request(http):
            response = http.post(
                "https://openrouter.ai/api/v1/audio/transcriptions",
                headers={"Authorization": "Bearer " + key.get_secret_value()},
                json=payload,
            )
            if response.status_code in (401, 403):
                raise TranscriptionError("OpenRouter rechazó la autorización. Revisa la clave y sus permisos; el original se conserva.")
            if response.status_code == 402:
                raise TranscriptionError("OpenRouter requiere saldo disponible; el original se conserva.")
            if response.status_code == 429:
                raise TranscriptionError("OpenRouter alcanzó su límite de solicitudes. Reintenta después; el original se conserva.")
            response.raise_for_status()
            result = response.json()
            text = result.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 100000:
                raise ValueError
            reported = result.get("usage")
            if isinstance(reported, dict):
                seconds = reported.get("seconds")
                if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > settings.audio_max_seconds:
                    raise TranscriptionError("El audio supera la duración permitida; el original se conserva.")
                if usage is not None:
                    for field in ("seconds", "cost", "total_tokens", "input_tokens", "output_tokens"):
                        value = reported.get(field)
                        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                            usage[field] = value
            return text.strip()

        if client is not None:
            return request(client)
        with httpx.Client(timeout=90, follow_redirects=False, trust_env=False) as http:
            return request(http)
    except TranscriptionError:
        raise
    except Exception:
        raise TranscriptionError("OpenRouter no pudo transcribir el audio. Revisa formato, modelo o disponibilidad; el original se conserva.") from None


def transcribe_audio(session, source_id, settings):
    try:
        with session.begin():
            audio = session.scalar(select(Source).where(Source.id == source_id).with_for_update())
            if audio is None or audio.source_type != "telegram_audio":
                raise TranscriptionError("La fuente no es un audio.")
            if audio.latest_transcript_source_id is not None:
                return audio.latest_transcript_source_id
            asset = session.scalar(select(AudioAsset).where(AudioAsset.source_id == source_id))
            if asset is None:
                raise TranscriptionError("El archivo original aún no está descargado.")
            if hashlib.sha256(asset.content).hexdigest() != asset.sha256:
                raise TranscriptionError("No se pudo verificar el archivo original.")
            usage = {}
            transcript = transcribe_bytes(asset.content, settings, mime_type=asset.mime_type, usage=usage)
            project = None if audio.raw_metadata.get("processing_schema") in {"action-plan-v1", "action-plan-v2"} else resolve_project(session, audio.raw_content + "\n" + transcript)
            transcript_id = uuid4()
            metadata = dict(audio.raw_metadata)
            metadata["transcription"] = {
                "provider": "openrouter", "model": settings.openrouter_stt_model,
                "language": settings.stt_language, "audio_source_id": str(audio.id),
                "audio_sha256": asset.sha256, "usage": usage,
            }
            if audio.raw_metadata.get("processing_schema") in {"action-plan-v1", "action-plan-v2"}:
                metadata["processing_schema"] = audio.raw_metadata["processing_schema"]
            child = Source(
                id=transcript_id, parent_source_id=audio.id, source_type="audio_transcript",
                raw_content=transcript, raw_metadata=metadata, received_at=audio.received_at,
                primary_project_id=project.id if project else None, processing_status="pending",
            )
            session.add(child)
            session.flush()
            audio.latest_transcript_source_id = transcript_id
            audio.primary_project_id = child.primary_project_id
            audio.processing_status = "transcribed"
            return transcript_id
    except TranscriptionError:
        with session.begin():
            audio = session.scalar(select(Source).where(Source.id == source_id).with_for_update())
            if audio is not None and audio.source_type == "telegram_audio" and audio.latest_transcript_source_id is None:
                audio.processing_status = "transcription_failed"
        raise
