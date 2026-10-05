from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity


class Company(Identity, Base):
    __tablename__ = "companies"
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(200), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
