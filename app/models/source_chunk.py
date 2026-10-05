from uuid import UUID
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import VECTOR
from app.models.base import Base, Identity


class SourceChunk(Identity, Base):
    __tablename__ = "source_chunks"
    __table_args__ = (
        UniqueConstraint("source_id", "chunk_version", "chunk_index"),
        CheckConstraint("chunk_index >= 0", name="nonnegative_index"),
        CheckConstraint("embedding IS NULL OR (embedding_dimensions IS NOT NULL AND vector_dims(embedding) = embedding_dimensions)", name="embedding_dimensions"),
    )
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), index=True)
    chunk_version: Mapped[str] = mapped_column(String(120))
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    chunk_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR())
    embedding_model: Mapped[str | None] = mapped_column(String(200), index=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer)
