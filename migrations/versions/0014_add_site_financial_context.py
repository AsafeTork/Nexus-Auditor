"""
Orphaned migration - placeholder

This is a stub migration created to fix a mismatch between the database
and the migration files. The original migration was deleted from the codebase
but still exists in the database.

Revision ID: 0014_add_site_financial_context
Revises: 0013_add_org_llm_provider_and_key
Create Date: 2026-05-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_add_site_financial_context"
down_revision = "0013_add_org_llm_provider_and_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This migration was previously removed from the codebase.
    # It should already be applied to the database, so nothing to do.
    pass


def downgrade() -> None:
    # Cannot downgrade - original migration code is lost
    raise NotImplementedError(
        "Cannot downgrade past migration 0014_add_site_financial_context. "
        "This migration was removed from the codebase. "
        "To fix this, manually remove the migration entry from alembic_version table."
    )
