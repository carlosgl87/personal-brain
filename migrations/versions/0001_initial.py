"""Esquema inicial aditivo de Personal Brain."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "areas",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(200), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.UniqueConstraint("slug", name="uq_areas_slug"),
    )
    op.create_table(
        "companies",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(200), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.UniqueConstraint("slug", name="uq_companies_slug"),
    )
    op.create_table(
        "categories",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("area_id", sa.Uuid(), sa.ForeignKey("areas.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(200), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.UniqueConstraint("area_id", "slug", name="uq_categories_area_id"),
        sa.UniqueConstraint("id", "area_id", name="uq_categories_id"),
    )
    op.create_index("ix_categories_area_id", "categories", ["area_id"])
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("area_id", sa.Uuid(), sa.ForeignKey("areas.id"), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("companies.id"), nullable=True),
        sa.Column("name", sa.String(250), nullable=False),
        sa.Column("slug", sa.String(250), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(50), server_default="active", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("area_id", "slug", name="uq_projects_area_id"),
        sa.ForeignKeyConstraint(["category_id", "area_id"], ["categories.id", "categories.area_id"]),
    )
    op.create_index("ix_projects_area_id", "projects", ["area_id"])
    op.create_index("ix_projects_category_id", "projects", ["category_id"])
    op.create_index("ix_projects_company_id", "projects", ["company_id"])
    op.create_index("ix_projects_status", "projects", ["status"])
    op.create_table(
        "project_aliases",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("alias", sa.String(250), nullable=False),
        sa.Column("normalized_alias", sa.String(250), nullable=False),
        sa.UniqueConstraint("project_id", "alias", name="uq_project_aliases_project_id"),
    )
    op.create_index("ix_project_aliases_project_id", "project_aliases", ["project_id"])
    op.create_index("ix_project_aliases_normalized_alias", "project_aliases", ["normalized_alias"])
    op.create_table(
        "sources",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_type", sa.String(80), nullable=False),
        sa.Column("raw_content", sa.Text(), nullable=False),
        sa.Column("raw_metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("primary_project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("external_source", sa.String(100), nullable=True),
        sa.Column("external_id", sa.String(250), nullable=True),
        sa.Column("processing_status", sa.String(50), server_default="pending", nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sources_primary_project_id", "sources", ["primary_project_id"])
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_text", sa.String(250), nullable=True),
        sa.Column("status", sa.String(50), server_default="open", nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tasks_source_id", "tasks", ["source_id"])
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])
    op.create_table(
        "decisions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id"), nullable=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("decision_text", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_decisions_source_id", "decisions", ["source_id"])
    op.create_index("ix_decisions_project_id", "decisions", ["project_id"])
    op.execute("""
        CREATE FUNCTION personal_brain_preserve_source() RETURNS trigger
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
               OR NEW.received_at IS DISTINCT FROM OLD.received_at THEN
                RAISE EXCEPTION 'Original source fields are immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER preserve_original_source
        BEFORE UPDATE OR DELETE ON sources
        FOR EACH ROW EXECUTE FUNCTION personal_brain_preserve_source()
    """)


def downgrade():
    raise RuntimeError("Downgrade deshabilitado para preservar datos de producción.")
