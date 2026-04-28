"""add org llm defaults

Revision ID: 0004_add_org_llm_defaults
Revises: 0003_audit_events_ts_ms_bigint
Create Date: 2026-04-28
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_add_org_llm_defaults"
down_revision = "0003_audit_events_ts_ms_bigint"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orgs", sa.Column("llm_base_url_v1", sa.String(length=1000), nullable=False, server_default=""))
    op.add_column("orgs", sa.Column("llm_model", sa.String(length=200), nullable=False, server_default=""))


def downgrade():
    op.drop_column("orgs", "llm_model")
    op.drop_column("orgs", "llm_base_url_v1")

