from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from app.api.routes import health, projects, telegram, processing

app = FastAPI(title="Personal Brain", version="0.5.0")
app.include_router(health.router)
app.include_router(projects.router)
app.include_router(telegram.router)
app.include_router(processing.router)


@app.exception_handler(SQLAlchemyError)
async def database_error(request: Request, exc: SQLAlchemyError):
    return JSONResponse(status_code=503, content={"detail": "Base de datos no disponible."})
