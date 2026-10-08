"""Check missing embeddings within the requested scope/window, only when search is needed."""
from sqlalchemy import select, exists, or_
from app.models import Source, SourceChunk, ChunkEmbedding
from app.services.memory import logical_chunks
from app.services.chunking import TEXT_SOURCE_TYPES
from app.services.queries import current_source, source_project_membership


def semantic_index_incomplete(session, scope, settings, plan):
    has_chunks = exists(select(SourceChunk.id).where(SourceChunk.source_id == Source.id, logical_chunks(settings)))
    missing_vector = exists(select(SourceChunk.id).where(SourceChunk.source_id == Source.id,
        logical_chunks(settings), ~exists(select(ChunkEmbedding.id).where(
            ChunkEmbedding.source_chunk_id == SourceChunk.id, ChunkEmbedding.provider == 'openrouter',
            ChunkEmbedding.model == settings.openrouter_embedding_model,
            ChunkEmbedding.dimensions == settings.openrouter_embedding_dimensions))))
    statement = select(Source.id).where(Source.source_type.in_(TEXT_SOURCE_TYPES), Source.raw_content != '',
        current_source(), or_(~has_chunks, missing_vector))
    if scope.project_ids is not None:
        statement = statement.where(source_project_membership(scope.project_ids))
    if plan.time_basis in {'source_date','created_date','mixed'}:
        column = Source.created_at if plan.time_basis == 'created_date' else Source.received_at
        if plan.date_from: statement = statement.where(column >= plan.date_from)
        if plan.date_to: statement = statement.where(column <= plan.date_to)
    return session.scalar(statement.limit(1)) is not None
