"""Versioned event-driven project memory; no startup or paid backfill."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_project_memory"
down_revision = "0008_vector_generations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("project_memory_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("previous_version_id", sa.Uuid(), sa.ForeignKey("project_memory_versions.id"), nullable=True),
        sa.Column("memory", JSONB(), nullable=False),
        sa.Column("update_type", sa.String(50), nullable=False),
        sa.Column("trigger_source_ids", JSONB(), nullable=False),
        sa.Column("retrieved_context", JSONB(), nullable=False),
        sa.Column("through_revision", sa.BigInteger(), nullable=False),
        sa.Column("consolidated_through_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.UniqueConstraint("project_id", "version_number"))
    op.create_index("ix_project_memory_versions_project_id", "project_memory_versions", ["project_id"])
    op.create_table("project_memory_state",
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("current_version_id", sa.Uuid(), sa.ForeignKey("project_memory_versions.id"), nullable=True),
        sa.Column("is_dirty", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("dirty_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_change_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("change_revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_incremental_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconciliation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_revision", sa.BigInteger(), server_default="0", nullable=False))
    op.create_index("ix_project_memory_state_refresh_after", "project_memory_state", ["refresh_after"])
    op.create_table("project_memory_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("origin_key", sa.String(200), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("command_source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=True),
        sa.UniqueConstraint("project_id", "revision", name="uq_project_memory_event_revision"), sa.UniqueConstraint("project_id", "origin_key", name="uq_project_memory_event_origin"))
    op.create_index("ix_project_memory_events_project_id", "project_memory_events", ["project_id"])
    for table in ("project_memory_versions", "project_memory_events"):
        op.execute("CREATE TRIGGER preserve_project_memory_history BEFORE UPDATE OR DELETE ON " + table +
                   " FOR EACH ROW EXECUTE FUNCTION personal_brain_preserve_extraction_evidence()")


def downgrade():
    raise RuntimeError("Downgrade destructivo deshabilitado.")
