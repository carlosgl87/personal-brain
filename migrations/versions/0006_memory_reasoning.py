"""Memoria derivada y razonamiento; sin backfill ni cambios en fuentes."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import VECTOR

revision = "0006_memory_reasoning"
down_revision = "0005_task_changes"
branch_labels = None
depends_on = None


def upgrade():
    # Error claro si el servidor no tiene pgvector instalado o permisos suficientes.
    op.execute("""
        DO $$ BEGIN
            CREATE EXTENSION IF NOT EXISTS vector;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'No se pudo habilitar pgvector. Instala la extension vector y verifica permisos antes de continuar.';
        END $$;
    """)
    op.create_table(
        "source_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("chunk_version", sa.String(120), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=False),
        sa.Column("embedding", VECTOR(), nullable=True),
        sa.Column("embedding_model", sa.String(200), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_id", "chunk_version", "chunk_index"),
        sa.CheckConstraint("chunk_index >= 0", name="nonnegative_index"),
        sa.CheckConstraint("embedding IS NULL OR (embedding_dimensions IS NOT NULL AND vector_dims(embedding) = embedding_dimensions)", name="embedding_dimensions"),
    )
    op.create_index("ix_source_chunks_source_id", "source_chunks", ["source_id"])
    op.create_index("ix_source_chunks_embedding_model", "source_chunks", ["embedding_model"])
    op.create_table(
        "reasoning_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("question_source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("planner_version", sa.String(100), nullable=False),
        sa.Column("plan", JSONB(), nullable=False),
        sa.Column("retrieved_context", JSONB(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_reasoning_runs_question_source_id", "reasoning_runs", ["question_source_id"])


def downgrade():
    raise RuntimeError("Downgrade destructivo deshabilitado.")
