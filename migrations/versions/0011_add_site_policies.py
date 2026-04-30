"""add site policies for safety gating

Revision ID: 0011_add_site_policies
Revises: 0010_add_learning_store
Create Date: 2026-04-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0011_add_site_policies"
down_revision = "0010_add_learning_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_policies",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("org_id", sa.String(length=36), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("site_id", sa.String(length=36), sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("allowed_action_kinds_json", sa.Text(), server_default=""),
        sa.Column("forbidden_action_kinds_json", sa.Text(), server_default=""),
        sa.Column("max_risk_level", sa.String(length=16), server_default="HIGH"),
        sa.Column("allow_auto_apply", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("enforce_csp_report_only", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("max_rate_limit_rps", sa.Integer(), server_default="20"),
        sa.Column("created_utc", sa.String(length=40)),
        sa.Column("updated_utc", sa.String(length=40)),
    )
    op.create_index("ix_site_policies_org_id", "site_policies", ["org_id"], unique=False)
    op.create_index("ix_site_policies_site_id", "site_policies", ["site_id"], unique=False)


def downgrade() -> None:
    try:
        op.drop_index("ix_site_policies_site_id", table_name="site_policies")
        op.drop_index("ix_site_policies_org_id", table_name="site_policies")
        op.drop_table("site_policies")
    except Exception:
        pass

