"""Identidad única de updates Telegram; no modifica ni borra fuentes."""
from alembic import op
import sqlalchemy as sa

revision = "0002_telegram_identity"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "uq_sources_telegram_external_id", "sources",
        ["external_source", "external_id"], unique=True,
        postgresql_where=sa.text("external_source = 'telegram' AND external_id IS NOT NULL"),
    )


def downgrade():
    raise RuntimeError("Downgrade deshabilitado para preservar garantías de idempotencia.")
