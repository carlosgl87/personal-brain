"""Fragmentos exactos y determinísticos, sin normalizar el texto original."""
import hashlib
from dataclasses import dataclass

TEXT_SOURCE_TYPES = ("telegram_text", "audio_transcript", "meeting_transcript", "manual_note", "document_text")


@dataclass(frozen=True)
class Chunk:
    index: int
    content: str
    start: int
    end: int


def chunk_version(settings):
    fingerprint = "|".join((str(settings.memory_chunk_size), str(settings.memory_chunk_overlap)))
    return settings.memory_chunk_version + "-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:16]


def legacy_chunk_version(settings):
    fingerprint = "|".join((str(settings.memory_chunk_size), str(settings.memory_chunk_overlap),
                           settings.openrouter_embedding_model or "none",
                           str(settings.openrouter_embedding_dimensions)))
    return settings.memory_chunk_version + "-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:16]


def chunk_text(text, size=6000, overlap=350):
    if not 0 <= overlap < size // 2 or size < 100:
        raise ValueError("Tamaño y overlap no válidos.")
    if not text.strip():
        return []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            floor = start + size // 2
            for separator in ("\n\n", "\n", ". ", "? ", "! ", " "):
                boundary = text.rfind(separator, floor, end)
                if boundary >= floor:
                    end = boundary + len(separator)
                    break
        chunks.append(Chunk(len(chunks), text[start:end], start, end))
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def chunk_source(source, settings):
    if source.source_type not in TEXT_SOURCE_TYPES:
        return []
    return chunk_text(source.raw_content, settings.memory_chunk_size, settings.memory_chunk_overlap)
