from uuid import UUID
from sqlalchemy import ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity


class AudioAsset(Identity, Base):
    __tablename__ = "audio_assets"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), unique=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(100))
