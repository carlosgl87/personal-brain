from datetime import datetime
from uuid import UUID
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity


class Source(Identity, Base):
    __tablename__ = "sources"
    __table_args__ = (Index(
        "uq_sources_telegram_external_id", "external_source", "external_id", unique=True,
        postgresql_where=text("external_source = 'telegram' AND external_id IS NOT NULL"),
    ),)
    parent_source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id"), index=True)
    latest_transcript_source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id"), index=True)
    latest_processing_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(80))
    raw_content: Mapped[str] = mapped_column(Text)
    raw_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    primary_project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    external_source: Mapped[str | None] = mapped_column(String(100))
    external_id: Mapped[str | None] = mapped_column(String(250))
    processing_status: Mapped[str] = mapped_column(String(50), default="pending", server_default="pending")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
