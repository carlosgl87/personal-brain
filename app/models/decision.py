from datetime import datetime
from uuid import UUID
from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity


class Decision(Identity, Base):
    __tablename__ = "decisions"
    processing_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id"), index=True)
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    decision_text: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
