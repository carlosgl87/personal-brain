"""Future Action Plans; no data rewrites or backfill."""
from alembic import op
import sqlalchemy as sa

revision = "0012_action_plans"
down_revision = "0011_task_completion_attempts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("project_updates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("processing_run_id", sa.Uuid(), sa.ForeignKey("processing_runs.id"), nullable=False),
        sa.Column("update_text", sa.Text(), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=True))
    for field in ("project_id", "source_id", "processing_run_id", "event_at"):
        op.create_index("ix_project_updates_" + field, "project_updates", [field])
    op.create_table("update_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("update_id", sa.Uuid(), sa.ForeignKey("project_updates.id"), nullable=False),
        sa.Column("source_chunk_id", sa.Uuid(), sa.ForeignKey("source_chunks.id"), nullable=False),
        sa.Column("processing_run_part_id", sa.Uuid(), sa.ForeignKey("processing_run_parts.id"), nullable=False),
        sa.Column("generation_part_id", sa.Uuid(), sa.ForeignKey("extraction_generation_parts.id"), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.UniqueConstraint("update_id", "source_chunk_id", "char_start", "char_end"),
        sa.CheckConstraint("char_start >= 0 AND char_end > char_start", name="valid_offsets"))
    for field in ("update_id", "source_chunk_id"):
        op.create_index("ix_update_evidence_" + field, "update_evidence", [field])
    for table in ("project_updates", "update_evidence"):
        op.execute("CREATE TRIGGER preserve_" + table + " BEFORE UPDATE OR DELETE ON " + table +
                   " FOR EACH ROW EXECUTE FUNCTION personal_brain_preserve_extraction_evidence()")
    op.drop_constraint("uq_task_changes_command_source_id", "task_changes", type_="unique")
    op.create_unique_constraint("uq_task_changes_command_source_id_task_id", "task_changes",
                                ["command_source_id", "task_id"])


def downgrade():
    raise RuntimeError("Downgrade destructivo deshabilitado.")
