from datetime import datetime
from uuid import UUID
from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base, Identity, Updated


class Project(Identity, Updated, Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("area_id", "slug"),
        ForeignKeyConstraint(["category_id", "area_id"], ["categories.id", "categories.area_id"]),
    )
    area_id: Mapped[UUID] = mapped_column(ForeignKey("areas.id"), index=True)
    category_id: Mapped[UUID | None] = mapped_column(index=True)
    company_id: Mapped[UUID | None] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(250))
    slug: Mapped[str] = mapped_column(String(250))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="active", server_default="active", index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    area: Mapped["Area"] = relationship(foreign_keys=[area_id])
    category: Mapped["Category | None"] = relationship(foreign_keys=[category_id, area_id], overlaps="area")
    company: Mapped["Company | None"] = relationship()
    aliases: Mapped[list["ProjectAlias"]] = relationship(order_by="ProjectAlias.alias")
