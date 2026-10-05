from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict


class CatalogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    slug: str


class AliasRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    alias: str
    normalized_alias: str


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    slug: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    area: CatalogRead
    category: CatalogRead | None
    company: CatalogRead | None
    aliases: list[AliasRead]
