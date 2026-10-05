"""Seed aditivo: no borra ni actualiza información existente."""
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.database import get_engine
from app.models import Area, Category, Company, Project, ProjectAlias
from app.services.normalization import normalize, slugify


def ensure(session, model, keys, values):
    session.execute(
        insert(model).values(id=uuid4(), **keys, **values)
        .on_conflict_do_nothing(index_elements=list(keys))
    )
    return session.scalar(select(model).filter_by(**keys))


def seed(session: Session):
    data = json.loads(Path(__file__).with_name("seed_data.json").read_text(encoding="utf-8"))
    areas = {
        name: ensure(session, Area, {"slug": slugify(name)}, {"name": name})
        for name in data["areas"]
    }
    companies = {
        name: ensure(session, Company, {"slug": slugify(name)}, {"name": name})
        for name in data["companies"]
    }
    categories = {
        name: ensure(session, Category,
                     {"area_id": areas["Laureate"].id, "slug": slugify(name)}, {"name": name})
        for name in data["categories"]
    }
    for item in data["projects"]:
        project = ensure(
            session, Project, {"area_id": areas[item["area"]].id, "slug": item["slug"]},
            {"name": item["name"],
             "category_id": categories[item["category"]].id if item.get("category") else None,
             "company_id": companies[item["company"]].id if item.get("company") else None},
        )
        for alias in item["aliases"]:
            ensure(session, ProjectAlias, {"project_id": project.id, "alias": alias},
                   {"normalized_alias": normalize(alias)})


def main():
    try:
        with Session(get_engine()) as session, session.begin():
            seed(session)
    except Exception:
        raise SystemExit("Seed no completado; transacción revertida. Verifica configuración y migraciones.") from None
    print("Seed completado: catálogo inicial asegurado sin borrar datos existentes.")


if __name__ == "__main__":
    main()
