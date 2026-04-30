"""add site contexts for contextual risk

Revision ID: 0012_add_site_contexts
Revises: 0011_add_site_policies
Create Date: 2026-04-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0012_add_site_contexts"
down_revision = "0011_add_site_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("site_contexts"):
        op.create_table(
            "site_contexts",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("org_id", sa.String(length=36), sa.ForeignKey("orgs.id"), nullable=False),
            sa.Column("site_id", sa.String(length=36), sa.ForeignKey("sites.id"), nullable=False),
            sa.Column("complexity", sa.String(length=16), server_default="MEDIUM"),
            sa.Column("coverage_quality", sa.String(length=16), server_default="MEDIUM"),
            sa.Column("instability_score", sa.Integer(), server_default="0"),
            sa.Column("last_updated_utc", sa.String(length=40)),
            sa.Column("created_utc", sa.String(length=40)),
        )
        insp = sa.inspect(bind)
    idx = {ix["name"] for ix in insp.get_indexes("site_contexts")} if insp.has_table("site_contexts") else set()
    if "ix_site_contexts_org_id" not in idx:
        op.create_index("ix_site_contexts_org_id", "site_contexts", ["org_id"], unique=False)
    if "ix_site_contexts_site_id" not in idx:
        op.create_index("ix_site_contexts_site_id", "site_contexts", ["site_id"], unique=False)


def downgrade() -> None:
    try:
        op.drop_index("ix_site_contexts_site_id", table_name="site_contexts")
        op.drop_index("ix_site_contexts_org_id", table_name="site_contexts")
        op.drop_table("site_contexts")
    except Exception:
        pass
