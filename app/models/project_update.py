from datetime import datetime
from uuid import UUID
from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity
from app.models.extraction_evidence import EvidenceFields
from sqlalchemy import CheckConstraint, UniqueConstraint


class ProjectUpdate(Identity, Base):
    __tablename__ = "project_updates"
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), index=True)
    processing_run_id: Mapped[UUID] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    update_text: Mapped[str] = mapped_column(Text)
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class UpdateEvidence(Identity, EvidenceFields, Base):
    __tablename__ = "update_evidence"
    __table_args__ = (UniqueConstraint("update_id", "source_chunk_id", "char_start", "char_end"),
                     CheckConstraint("char_start >= 0 AND char_end > char_start", name="valid_offsets"))
    update_id: Mapped[UUID] = mapped_column(ForeignKey("project_updates.id"), index=True)
