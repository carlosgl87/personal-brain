from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Project
from app.services.normalization import normalize


def match_project(text: str, projects) -> Project | None:
    """Coincidencias por palabras completas; varios proyectos => sin asociación."""
    content = " " + normalize(text) + " "
    matches = {}
    for project in projects:
        names = [project.name, project.slug, *(alias.alias for alias in project.aliases)]
        for name in names:
            normalized = normalize(name)
            if normalized and " " + normalized + " " in content:
                matches[project.id] = project
                break
    return next(iter(matches.values())) if len(matches) == 1 else None


def resolve_project(session: Session, text: str) -> Project | None:
    projects = session.scalars(
        select(Project).where(Project.status == "active", Project.archived_at.is_(None))
        .options(selectinload(Project.aliases))
    ).all()
    return match_project(text, projects)


def allows_content_project(source):
    """An explicit document caption is authoritative, including an unresolved target."""
    metadata = source.raw_metadata or {}
    message = metadata.get("message") or metadata.get("edited_message") or {}
    return not (isinstance(message, dict) and isinstance(message.get("document"), dict)
                and isinstance(message.get("caption"), str) and message["caption"].strip())
