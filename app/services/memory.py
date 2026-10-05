"""Indexación por fuente con commits pequeños; búsqueda exacta y provenance."""
from uuid import uuid4

from sqlalchemy import exists, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.models import Source, SourceChunk
from app.services.chunking import TEXT_SOURCE_TYPES, chunk_source, chunk_version
from app.services.embeddings import embed_texts, embedding_ready
from app.services.normalization import normalize
from app.services.queries import current_source, resolve_scope


class MemoryError(RuntimeError):
    pass


def index_source(session, source_id, settings):
    version = chunk_version(settings)
    # Chunks se confirman antes de cualquier llamada pagada.
    with session.begin():
        source = session.scalar(select(Source).where(Source.id == source_id).with_for_update())
        if source is None:
            raise MemoryError("Fuente inexistente.")
        for chunk in chunk_source(source, settings):
            session.execute(insert(SourceChunk).values(
                id=uuid4(), source_id=source_id, chunk_version=version, chunk_index=chunk.index,
                content=chunk.content, char_start=chunk.start, char_end=chunk.end,
                chunk_metadata={"algorithm": settings.memory_chunk_version,
                                "requested_embedding_model": settings.openrouter_embedding_model,
                                "dimensions": settings.openrouter_embedding_dimensions},
            ).on_conflict_do_nothing(index_elements=["source_id", "chunk_version", "chunk_index"]))
    with session.begin():
        ids = list(session.scalars(select(SourceChunk.id).where(
            SourceChunk.source_id == source_id, SourceChunk.chunk_version == version,
            SourceChunk.embedding.is_(None),
        ).order_by(SourceChunk.chunk_index)))
    completed = 0
    if embedding_ready(settings):
        for chunk_id in ids:
            # Bloqueo serializa reintentos concurrentes; solo una llamada por chunk exitoso.
            with session.begin():
                chunk = session.scalar(select(SourceChunk).where(SourceChunk.id == chunk_id).with_for_update())
                if chunk.embedding is not None:
                    continue
                vector = embed_texts([chunk.content], settings)[0]
                chunk.embedding = vector
                chunk.embedding_model = settings.openrouter_embedding_model
                chunk.embedding_dimensions = settings.openrouter_embedding_dimensions
                completed += 1
    return {"source_id": str(source_id), "version": version, "embedded": completed,
            "pending_embeddings": 0 if embedding_ready(settings) else len(ids)}


def backfill_candidates(session, settings, limit):
    if not 1 <= limit <= 100:
        raise MemoryError("Limit debe estar entre 1 y 100.")
    version = chunk_version(settings)
    has_chunks = exists(select(SourceChunk.id).where(
        SourceChunk.source_id == Source.id, SourceChunk.chunk_version == version))
    incomplete = exists(select(SourceChunk.id).where(
        SourceChunk.source_id == Source.id, SourceChunk.chunk_version == version,
        SourceChunk.embedding.is_(None)))
    return list(session.scalars(select(Source.id).where(
        Source.source_type.in_(TEXT_SOURCE_TYPES), Source.raw_content != "", current_source(),
        or_(~has_chunks, incomplete) if embedding_ready(settings) else ~has_chunks,
    ).order_by(Source.received_at, Source.id).limit(limit)))


def filtered_scope(session, project=None, company=None, area=None):
    allowed = None
    for kind, name in (("proyecto", project), ("empresa", company), ("area", area)):
        if name:
            scope = resolve_scope(session, kind + " " + normalize(name))
            if scope.error:
                raise MemoryError(scope.error)
            allowed = set(scope.project_ids) if allowed is None else allowed & set(scope.project_ids)
    return allowed


def semantic_statement(vector, settings, project_ids=None, source_type=None, date_from=None, date_to=None):
    distance = SourceChunk.embedding.cosine_distance(vector)
    statement = select(SourceChunk, Source, distance.label("distance")).join(Source, Source.id == SourceChunk.source_id).where(
        SourceChunk.chunk_version == chunk_version(settings),
        SourceChunk.embedding_model == settings.openrouter_embedding_model,
        SourceChunk.embedding_dimensions == settings.openrouter_embedding_dimensions,
        SourceChunk.embedding.is_not(None), Source.source_type.in_(TEXT_SOURCE_TYPES), current_source(),
    )
    if project_ids is not None:
        statement = statement.where(Source.primary_project_id.in_(project_ids))
    if source_type:
        statement = statement.where(Source.source_type == source_type)
    if date_from:
        statement = statement.where(Source.received_at >= date_from)
    if date_to:
        statement = statement.where(Source.received_at <= date_to)
    return statement.order_by(distance, Source.received_at.desc(), SourceChunk.id)


def semantic_search(session, query, settings, *, project=None, company=None, area=None,
                    source_type=None, limit=5, project_ids=None, date_from=None, date_to=None):
    if not query.strip() or len(query) > 3000 or not 1 <= limit <= 12:
        raise MemoryError("Consulta o límite no válido.")
    if source_type and source_type not in TEXT_SOURCE_TYPES:
        raise MemoryError("Tipo de fuente no textual.")
    scopes = filtered_scope(session, project, company, area)
    if scopes is not None:
        project_ids = list(scopes if project_ids is None else scopes & set(project_ids))
    if project_ids == []:
        return []
    vector = embed_texts([query], settings)[0]
    rows = session.execute(semantic_statement(vector, settings, project_ids, source_type, date_from, date_to).limit(limit)).all()
    return [{
        "chunk_id": str(chunk.id), "source_id": str(source.id),
        "project_id": str(source.primary_project_id) if source.primary_project_id else None,
        "source_type": source.source_type, "received_at": source.received_at.isoformat(),
        "content": chunk.content, "char_start": chunk.char_start, "char_end": chunk.char_end,
        "distance": float(distance), "score": 1 - float(distance),
        "chunk_version": chunk.chunk_version, "embedding_model": chunk.embedding_model,
    } for chunk, source, distance in rows]


def maybe_index(session, source_id, settings):
    if settings.memory_auto_index is not True:
        return
    try:
        index_source(session, source_id, settings)
    except Exception:
        session.rollback()
        print("Fuente conservada; memoria pendiente. Reintenta con python -m app.memory source UUID.")
