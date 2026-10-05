from uuid import UUID
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity


class TaskChange(Identity, Base):
    __tablename__ = "task_changes"
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), index=True)
    command_source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), unique=True)
    action: Mapped[str] = mapped_column(String(50))
    before: Mapped[dict] = mapped_column(JSONB)
    after: Mapped[dict] = mapped_column(JSONB)
    answer: Mapped[str] = mapped_column(String(2000))
