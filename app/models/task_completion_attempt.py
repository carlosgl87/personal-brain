from uuid import UUID

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Identity


class TaskCompletionAttempt(Identity, Base):
    __tablename__ = "task_completion_attempts"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), unique=True)
    result: Mapped[dict] = mapped_column(JSONB)
    answer: Mapped[str] = mapped_column(Text)
