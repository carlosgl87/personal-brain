from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from app.database import get_engine

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/health/db")
def database_health():
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=503, detail="Base de datos no disponible.") from None
    return {"status": "ok"}
