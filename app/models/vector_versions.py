"""Separate vector versions and explicit paid extraction generations."""
from datetime import datetime
from uuid import UUID
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import VECTOR
from app.models.base import Base, Identity


class ChunkEmbedding(Identity, Base):
    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        UniqueConstraint("source_chunk_id", "provider", "model", "dimensions"),
        CheckConstraint("dimensions > 0 AND vector_dims(embedding) = dimensions", name="valid_dimensions"),
    )
    source_chunk_id: Mapped[UUID] = mapped_column(ForeignKey("source_chunks.id"), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200), index=True)
    dimensions: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(VECTOR())


class ExtractionGeneration(Identity, Base):
    __tablename__ = "extraction_generations"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), index=True)
    chunk_version: Mapped[str] = mapped_column(String(120))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100))
    previous_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("processing_runs.id"))
    status: Mapped[str] = mapped_column(String(50), default="pending")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GenerationPart(Identity, Base):
    __tablename__ = "extraction_generation_parts"
    __table_args__ = (UniqueConstraint("generation_id", "source_chunk_id"),)
    generation_id: Mapped[UUID] = mapped_column(ForeignKey("extraction_generations.id"), index=True)
    base_part_id: Mapped[UUID] = mapped_column(ForeignKey("processing_run_parts.id"))
    source_chunk_id: Mapped[UUID] = mapped_column(ForeignKey("source_chunks.id"), index=True)
    part_index: Mapped[int] = mapped_column(Integer)
    result: Mapped[dict] = mapped_column(JSONB)
