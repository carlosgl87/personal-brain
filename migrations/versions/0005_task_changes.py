"""Historial aditivo de cambios manuales de tareas."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_task_changes"
down_revision = "0004_audio_sources"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_changes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("command_source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("before", JSONB(), nullable=False),
        sa.Column("after", JSONB(), nullable=False),
        sa.Column("answer", sa.String(2000), nullable=False),
        sa.UniqueConstraint("command_source_id", name="uq_task_changes_command_source_id"),
    )
    op.create_index("ix_task_changes_task_id", "task_changes", ["task_id"])
    op.execute("""
        CREATE FUNCTION personal_brain_preserve_task_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Task change history is immutable';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER preserve_task_change
        BEFORE UPDATE OR DELETE ON task_changes
        FOR EACH ROW EXECUTE FUNCTION personal_brain_preserve_task_change()
    """)


def downgrade():
    raise RuntimeError("Downgrade destructivo deshabilitado.")
