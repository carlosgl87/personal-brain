from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.database import get_session
from app.models import Area, Category, Company, Project
from app.schemas.project import ProjectRead
from app.services.normalization import slugify

router = APIRouter(prefix="/projects", tags=["projects"])


def project_query():
    return select(Project).options(
        selectinload(Project.area), selectinload(Project.category),
        selectinload(Project.company), selectinload(Project.aliases),
    )


def catalog_filter(model, value):
    return or_(func.lower(model.name) == value.casefold(), model.slug == slugify(value))


@router.get("", response_model=list[ProjectRead])
def list_projects(
    area: str | None = None, category: str | None = None,
    company: str | None = None, status: str | None = None,
    session: Session = Depends(get_session),
):
    query = project_query()
    if area:
        query = query.join(Project.area).where(catalog_filter(Area, area))
    if category:
        query = query.join(Project.category).where(catalog_filter(Category, category))
    if company:
        query = query.join(Project.company).where(catalog_filter(Company, company))
    if status:
        query = query.where(Project.status == status)
    return session.scalars(query.order_by(Project.name, Project.id)).all()


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(project_id: UUID, session: Session = Depends(get_session)):
    project = session.scalar(project_query().where(Project.id == project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado.")
    return project
