from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Project
from app.services.normalization import normalize


def _phrase_matches(text, entries, names):
    content = " " + normalize(text) + " "
    return {entry.id: entry for entry in entries if any(
        normalize(name) and " " + normalize(name) + " " in content
        for name in names(entry))}


def _project_names(project):
    return [project.name, project.slug, *(alias.alias for alias in project.aliases)]


def _document_signal(text, projects, companies, areas):
    area_matches = _phrase_matches(text, areas, lambda a: [a.name, a.slug])
    company_matches = _phrase_matches(text, companies, lambda c: [c.name, c.slug])
    candidates = list(projects)
    if len(area_matches) == 1:
        candidates = [p for p in candidates if getattr(p, "area_id", None) in area_matches]
    if len(company_matches) == 1:
        candidates = [p for p in candidates if getattr(p, "company_id", None) in company_matches]
    return area_matches, company_matches, _phrase_matches(text, candidates, _project_names)


def resolve_document_project(filename, content, projects, companies, areas):
    """Exact phrases only; reconcile independent signals without guessing."""
    projects, companies, areas = list(projects), list(companies), list(areas)
    signals = [_document_signal(text, projects, companies, areas) for text in (filename, content)]
    area_ids = set().union(*(set(signal[0]) for signal in signals))
    company_ids = set().union(*(set(signal[1]) for signal in signals))
    if len(area_ids) > 1 or len(company_ids) > 1:
        return None
    matches = {}
    for _, _, candidates in signals:
        compatible = {key: p for key, p in candidates.items()
                      if (not area_ids or getattr(p, "area_id", None) in area_ids)
                      and (not company_ids or getattr(p, "company_id", None) in company_ids)}
        # A project signal outside the other signal's context is a conflict.
        if candidates and not compatible:
            return None
        if len(compatible) > 1:
            return None
        matches.update(compatible)
    return next(iter(matches.values())) if len(matches) == 1 else None


def match_source_project(source, projects):
    """Use the same document resolver during later extraction."""
    metadata = source.raw_metadata or {}
    message = metadata.get("message") or metadata.get("edited_message") or {}
    document = message.get("document") if isinstance(message, dict) else None
    if isinstance(document, dict):
        companies, areas = {}, {}
        for project in projects:
            for attribute, catalog in (("company", companies), ("area", areas)):
                entry = getattr(project, attribute, None)
                if entry is not None:
                    catalog[entry.id] = entry
        return resolve_document_project(document.get("file_name") or "", source.raw_content,
                                        projects, companies.values(), areas.values())
    return match_project(source.raw_content, projects)


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
