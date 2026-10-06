from datetime import datetime
from uuid import UUID
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity


class SourceProcessingJob(Identity, Base):
    __tablename__ = "source_processing_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('pending','processing','retry','completed','failed')", name="status"),
        CheckConstraint("attempts >= 0", name="attempts"),
        Index("ix_source_processing_jobs_ready", "status", "next_retry_at"),
    )
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None]
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(50))
