"""Immutable consolidated versions, lightweight state and committed change cursor."""
from datetime import datetime
from uuid import UUID
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity


class ProjectMemoryVersion(Identity, Base):
    __tablename__ = "project_memory_versions"
    __table_args__ = (UniqueConstraint("project_id", "version_number"),)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    previous_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("project_memory_versions.id"))
    memory: Mapped[dict] = mapped_column(JSONB)
    update_type: Mapped[str] = mapped_column(String(50))
    trigger_source_ids: Mapped[list] = mapped_column(JSONB)
    retrieved_context: Mapped[dict] = mapped_column(JSONB)
    through_revision: Mapped[int] = mapped_column(BigInteger)
    consolidated_through_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100))


class ProjectMemoryState(Base):
    __tablename__ = "project_memory_state"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    current_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("project_memory_versions.id"))
    is_dirty: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    dirty_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    change_revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    last_incremental_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciliation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class ProjectMemoryEvent(Identity, Base):
    __tablename__ = "project_memory_events"
    __table_args__ = (UniqueConstraint("project_id", "revision", name="uq_project_memory_event_revision"), UniqueConstraint("project_id", "origin_key", name="uq_project_memory_event_origin"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    revision: Mapped[int] = mapped_column(BigInteger)
    origin_key: Mapped[str] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(50))
    source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id"))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"))
    command_source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id"))
