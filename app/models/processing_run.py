from uuid import UUID
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity


class ProcessingRun(Identity, Base):
    __tablename__ = "processing_runs"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100))
    result: Mapped[dict] = mapped_column(JSONB)
