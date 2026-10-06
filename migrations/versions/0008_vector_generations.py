"""Preserve vectors and partials while adding independent versions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import VECTOR

revision = "0008_vector_generations"
down_revision = "0007_hierarchical_extraction"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("source_chunks", sa.Column("logical_version", sa.String(120), nullable=True))
    op.create_index("ix_source_chunks_logical_version", "source_chunks", ["logical_version"])
    op.create_table("chunk_embeddings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_chunk_id", sa.Uuid(), sa.ForeignKey("source_chunks.id"), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", VECTOR(), nullable=False),
        sa.UniqueConstraint("source_chunk_id", "provider", "model", "dimensions"),
        sa.CheckConstraint("dimensions > 0 AND vector_dims(embedding) = dimensions", name="valid_dimensions"))
    op.create_index("ix_chunk_embeddings_source_chunk_id", "chunk_embeddings", ["source_chunk_id"])
    op.create_index("ix_chunk_embeddings_model", "chunk_embeddings", ["model"])
    # Local copy only: no provider calls, no text rechunking and no overwrite.
    op.execute("""
        INSERT INTO chunk_embeddings (id, created_at, source_chunk_id, provider, model, dimensions, embedding)
        SELECT gen_random_uuid(), created_at, id, 'openrouter', embedding_model, embedding_dimensions, embedding
        FROM source_chunks
        WHERE embedding IS NOT NULL AND embedding_model IS NOT NULL AND embedding_dimensions IS NOT NULL
        ON CONFLICT (source_chunk_id, provider, model, dimensions) DO NOTHING
    """)
    op.create_table("extraction_generations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("chunk_version", sa.String(120), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("previous_run_id", sa.Uuid(), sa.ForeignKey("processing_runs.id"), nullable=True),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_extraction_generations_source_id", "extraction_generations", ["source_id"])
    op.create_table("extraction_generation_parts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), sa.ForeignKey("extraction_generations.id"), nullable=False),
        sa.Column("base_part_id", sa.Uuid(), sa.ForeignKey("processing_run_parts.id"), nullable=False),
        sa.Column("source_chunk_id", sa.Uuid(), sa.ForeignKey("source_chunks.id"), nullable=False),
        sa.Column("part_index", sa.Integer(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.UniqueConstraint("generation_id", "source_chunk_id"))
    op.create_index("ix_extraction_generation_parts_generation_id", "extraction_generation_parts", ["generation_id"])
    op.create_index("ix_extraction_generation_parts_source_chunk_id", "extraction_generation_parts", ["source_chunk_id"])
    for table in ("task_evidence", "decision_evidence"):
        op.add_column(table, sa.Column("generation_part_id", sa.Uuid(), nullable=True))
        op.create_foreign_key("fk_" + table + "_generation_part", table, "extraction_generation_parts", ["generation_part_id"], ["id"])
    op.execute("CREATE TRIGGER preserve_generation_part BEFORE UPDATE OR DELETE ON extraction_generation_parts FOR EACH ROW EXECUTE FUNCTION personal_brain_preserve_extraction_evidence()")


def downgrade():
    raise RuntimeError("Downgrade destructivo deshabilitado.")
