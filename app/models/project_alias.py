from uuid import UUID
from sqlalchemy import ForeignKey, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, Identity
from app.services.normalization import normalize


class ProjectAlias(Identity, Base):
    __tablename__ = "project_aliases"
    __table_args__ = (UniqueConstraint("project_id", "alias"),)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    alias: Mapped[str] = mapped_column(String(250))
    normalized_alias: Mapped[str] = mapped_column(String(250), index=True)


@event.listens_for(ProjectAlias, "before_insert")
@event.listens_for(ProjectAlias, "before_update")
def normalize_alias(mapper, connection, target):
    target.normalized_alias = normalize(target.alias)
