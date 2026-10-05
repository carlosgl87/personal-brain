from uuid import UUID
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity


class ReasoningRun(Identity, Base):
    __tablename__ = "reasoning_runs"
    question_source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id"), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200))
    planner_version: Mapped[str] = mapped_column(String(100))
    plan: Mapped[dict] = mapped_column(JSONB)
    retrieved_context: Mapped[dict] = mapped_column(JSONB)
    answer: Mapped[str] = mapped_column(Text)
