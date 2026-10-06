from uuid import UUID
from sqlalchemy import BigInteger, ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity


class DocumentAsset(Identity, Base):
    __tablename__ = "document_assets"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(100))
    file_size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    telegram_file_id: Mapped[str] = mapped_column(String(1024))
    telegram_file_unique_id: Mapped[str | None] = mapped_column(String(1024))
    original_bytes: Mapped[bytes] = mapped_column(LargeBinary)
