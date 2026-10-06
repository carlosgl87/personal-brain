from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from functools import lru_cache
from alembic.script import ScriptDirectory
from app.config import ROOT
from app.database import get_engine

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    return {"status": "ok"}


@lru_cache(maxsize=1)
def schema_heads():
    return set(ScriptDirectory(str(ROOT / "migrations")).get_heads())


@router.get("/health/db")
def database_health():
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
            revisions = set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all())
            if revisions != schema_heads():
                raise RuntimeError("Schema does not match this release")
    except Exception:
        raise HTTPException(status_code=503, detail="Base de datos no disponible.") from None
    return {"status": "ok"}
