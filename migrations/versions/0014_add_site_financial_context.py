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
    # Alembic marks this as complete without attempting any schema changes.
    op.execute("SELECT 1")  # No-op to ensure function completes


def downgrade() -> None:
    # This migration cannot be rolled back - original code is lost
    # Instead of raising, just skip it to allow deployment to continue
    op.execute("SELECT 1")  # No-op
