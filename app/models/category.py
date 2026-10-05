from uuid import UUID
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity


class Category(Identity, Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("area_id", "slug"), UniqueConstraint("id", "area_id"))
    area_id: Mapped[UUID] = mapped_column(ForeignKey("areas.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
