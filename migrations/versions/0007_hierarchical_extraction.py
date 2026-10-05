"""Resumable partial extraction and multi-chunk evidence; additive only."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_hierarchical_extraction"
down_revision = "0006_memory_reasoning"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "processing_run_parts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("source_chunk_id", sa.Uuid(), sa.ForeignKey("source_chunks.id"), nullable=False),
        sa.Column("chunk_version", sa.String(120), nullable=False),
        sa.Column("part_index", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.UniqueConstraint("source_id", "source_chunk_id", "chunk_version", "prompt_version", "model"),
        sa.CheckConstraint("part_index >= 0", name="nonnegative_index"),
    )
    op.create_index("ix_processing_run_parts_source_id", "processing_run_parts", ["source_id"])
    op.create_index("ix_processing_run_parts_source_chunk_id", "processing_run_parts", ["source_chunk_id"])
    for table, parent, field in (("task_evidence", "tasks", "task_id"),
                                  ("decision_evidence", "decisions", "decision_id")):
        op.create_table(
            table,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column(field, sa.Uuid(), sa.ForeignKey(parent + ".id"), nullable=False),
            sa.Column("source_chunk_id", sa.Uuid(), sa.ForeignKey("source_chunks.id"), nullable=False),
            sa.Column("processing_run_part_id", sa.Uuid(), sa.ForeignKey("processing_run_parts.id"), nullable=False),
            sa.Column("evidence", sa.Text(), nullable=False),
            sa.Column("char_start", sa.Integer(), nullable=False),
            sa.Column("char_end", sa.Integer(), nullable=False),
            sa.UniqueConstraint(field, "source_chunk_id", "char_start", "char_end"),
            sa.CheckConstraint("char_start >= 0 AND char_end > char_start", name="valid_offsets"),
        )
        op.create_index("ix_" + table + "_" + field, table, [field])
        op.create_index("ix_" + table + "_source_chunk_id", table, ["source_chunk_id"])
    op.execute("""
        CREATE FUNCTION personal_brain_preserve_extraction_evidence() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Extraction parts and evidence are immutable';
        END; $$
    """)
    for table in ("processing_run_parts", "task_evidence", "decision_evidence"):
        op.execute("CREATE TRIGGER preserve_extraction_evidence BEFORE UPDATE OR DELETE ON " + table +
                   " FOR EACH ROW EXECUTE FUNCTION personal_brain_preserve_extraction_evidence()")


def downgrade():
    raise RuntimeError("Downgrade destructivo deshabilitado para conservar partes y evidencias.")
