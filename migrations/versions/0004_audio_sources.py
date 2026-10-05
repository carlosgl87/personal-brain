"""Audio original inmutable y transcripción vinculada; migración aditiva."""
from alembic import op
import sqlalchemy as sa

revision = "0004_audio_sources"
down_revision = "0003_processing_runs"
branch_labels = None
depends_on = None


def upgrade():
    for column in ("parent_source_id", "latest_transcript_source_id"):
        op.add_column("sources", sa.Column(column, sa.Uuid(), nullable=True))
        op.create_foreign_key("fk_sources_" + column + "_sources", "sources", "sources", [column], ["id"])
        op.create_index("ix_sources_" + column, "sources", [column])
    op.create_table(
        "audio_assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.UniqueConstraint("source_id", name="uq_audio_assets_source_id"),
    )
    op.execute("""
        CREATE FUNCTION personal_brain_preserve_audio() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Original audio assets are immutable';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER preserve_original_audio
        BEFORE UPDATE OR DELETE ON audio_assets
        FOR EACH ROW EXECUTE FUNCTION personal_brain_preserve_audio()
    """)



    op.execute("""
        CREATE OR REPLACE FUNCTION personal_brain_preserve_source() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Original sources cannot be deleted';
            END IF;
            IF NEW.raw_content IS DISTINCT FROM OLD.raw_content
               OR NEW.raw_metadata IS DISTINCT FROM OLD.raw_metadata
               OR NEW.source_type IS DISTINCT FROM OLD.source_type
               OR NEW.external_source IS DISTINCT FROM OLD.external_source
               OR NEW.external_id IS DISTINCT FROM OLD.external_id
               OR NEW.received_at IS DISTINCT FROM OLD.received_at
               OR NEW.parent_source_id IS DISTINCT FROM OLD.parent_source_id THEN
                RAISE EXCEPTION 'Original source fields are immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)


def downgrade():
    raise RuntimeError("Downgrade deshabilitado para conservar audios y transcripciones.")
