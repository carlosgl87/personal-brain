"""Historial de extracción y referencias a datos derivados, sin borrar datos."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_processing_runs"
down_revision = "0002_telegram_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "processing_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_processing_runs_source_id", "processing_runs", ["source_id"])
    for table, column in [
        ("sources", "latest_processing_run_id"),
        ("tasks", "processing_run_id"), ("decisions", "processing_run_id"),
    ]:
        op.add_column(table, sa.Column(column, sa.Uuid(), nullable=True))
        op.create_foreign_key("fk_" + table + "_" + column + "_processing_runs",
                              table, "processing_runs", [column], ["id"])
        op.create_index("ix_" + table + "_" + column, table, [column])


def downgrade():
    raise RuntimeError("Downgrade deshabilitado para conservar historia de procesamiento.")
