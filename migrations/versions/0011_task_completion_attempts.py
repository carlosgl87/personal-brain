"""Immutable, idempotent decisions for natural Telegram task completion."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011_task_completion_attempts"
down_revision = "0010_documents_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("task_completion_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.UniqueConstraint("source_id"))
    op.execute("CREATE TRIGGER preserve_task_completion_attempt BEFORE UPDATE OR DELETE "
               "ON task_completion_attempts FOR EACH ROW "
               "EXECUTE FUNCTION personal_brain_preserve_extraction_evidence()")


def downgrade():
    raise RuntimeError("Downgrade destructivo deshabilitado.")
