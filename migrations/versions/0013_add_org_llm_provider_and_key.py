"""add org llm provider and api key

Revision ID: 0013_org_llm_provider
Revises: 0012_add_site_contexts
Create Date: 2026-04-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0013_org_llm_provider"
down_revision = "0012_add_site_contexts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orgs", sa.Column("llm_provider", sa.String(length=64), nullable=False, server_default="openai_compatible"))
    op.add_column("orgs", sa.Column("llm_api_key", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("orgs", "llm_api_key")
    op.drop_column("orgs", "llm_provider")
