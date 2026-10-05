"""Append-only successful partials and evidence for final structured items."""
from uuid import UUID
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity


class ProcessingRunPart(Identity, Base):
    __tablename__ = "processing_run_parts"
    __table_args__ = (
        UniqueConstraint("source_id", "source_chunk_id", "chunk_version", "prompt_version", "model"),
        CheckConstraint("part_index >= 0", name="nonnegative_index"),
    )
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), index=True)
    source_chunk_id: Mapped[UUID] = mapped_column(ForeignKey("source_chunks.id"), index=True)
    chunk_version: Mapped[str] = mapped_column(String(120))
    part_index: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100))
    result: Mapped[dict] = mapped_column(JSONB)


class EvidenceFields:
    source_chunk_id: Mapped[UUID] = mapped_column(ForeignKey("source_chunks.id"), index=True)
    processing_run_part_id: Mapped[UUID] = mapped_column(ForeignKey("processing_run_parts.id"))
    evidence: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)


class TaskEvidence(Identity, EvidenceFields, Base):
    __tablename__ = "task_evidence"
    __table_args__ = (
        UniqueConstraint("task_id", "source_chunk_id", "char_start", "char_end"),
        CheckConstraint("char_start >= 0 AND char_end > char_start", name="valid_offsets"),
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), index=True)


class DecisionEvidence(Identity, EvidenceFields, Base):
    __tablename__ = "decision_evidence"
    __table_args__ = (
        UniqueConstraint("decision_id", "source_chunk_id", "char_start", "char_end"),
        CheckConstraint("char_start >= 0 AND char_end > char_start", name="valid_offsets"),
    )
    decision_id: Mapped[UUID] = mapped_column(ForeignKey("decisions.id"), index=True)
