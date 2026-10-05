from alembic import context
from sqlalchemy import create_engine, pool
from app.config import get_settings
from app.models import Base

target_metadata = Base.metadata


def run():
    try:
        url = get_settings().database_url
        if context.is_offline_mode():
            context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                              dialect_opts={"paramstyle": "named"}, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
        else:
            engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True,
                                   connect_args={"connect_timeout": 10})
            with engine.connect() as connection:
                context.configure(connection=connection, target_metadata=target_metadata,
                                  compare_type=True)
                with context.begin_transaction():
                    context.run_migrations()
            engine.dispose()
    except Exception:
        raise RuntimeError("Migración no completada. Verifica conexión, permisos y esquema; detalles sensibles omitidos.") from None


run()
