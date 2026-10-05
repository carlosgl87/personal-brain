from datetime import datetime
from uuid import UUID
from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity, Updated


class Task(Identity, Updated, Base):
    __tablename__ = "tasks"
    processing_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id"), index=True)
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    owner_text: Mapped[str | None] = mapped_column(String(250))
    status: Mapped[str] = mapped_column(String(50), default="open", server_default="open")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
