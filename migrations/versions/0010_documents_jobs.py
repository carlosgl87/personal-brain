"""Immutable original documents and durable processing queue; additive only."""
from alembic import op
import sqlalchemy as sa

revision = "0010_documents_jobs"
down_revision = "0009_project_memory"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("document_assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("telegram_file_id", sa.String(1024), nullable=False),
        sa.Column("telegram_file_unique_id", sa.String(1024), nullable=True),
        sa.Column("original_bytes", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint("source_id"))
    op.execute("CREATE TRIGGER preserve_document_original BEFORE UPDATE OR DELETE ON document_assets "
               "FOR EACH ROW EXECUTE FUNCTION personal_brain_preserve_extraction_evidence()")
    op.create_table("source_processing_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(50), nullable=True),
        sa.UniqueConstraint("source_id"),
        sa.CheckConstraint("status IN ('pending','processing','retry','completed','failed')", name="status"),
        sa.CheckConstraint("attempts >= 0", name="attempts"))
    op.create_index("ix_source_processing_jobs_ready", "source_processing_jobs", ["status", "next_retry_at"])


def downgrade():
    raise RuntimeError("Downgrade destructivo deshabilitado.")
