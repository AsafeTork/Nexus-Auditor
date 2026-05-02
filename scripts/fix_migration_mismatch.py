#!/usr/bin/env python3
"""
Fix migration mismatch when database has migrations that don't exist in code.

This handles the case where:
1. A migration was deleted from the codebase
2. But it still exists in the database alembic_version table
3. Alembic can't resolve the chain

Solution: Create a stub migration that documents the orphaned migration.
"""

import os
import sys
from datetime import datetime

def create_stub_migration(migration_id, description="Orphaned migration - placeholder"):
    """Create a stub migration to fix the chain"""

    migrations_dir = "migrations/versions"
    timestamp = datetime.now().strftime("%Y-%m-%d")

    # Extract version number from ID
    version_num = migration_id.split('_')[0]

    filename = f"{migrations_dir}/{migration_id}.py"

    if os.path.exists(filename):
        print(f"Migration {filename} already exists")
        return False

    # Determine down_revision (previous migration)
    # List all existing migrations
    files = sorted([f for f in os.listdir(migrations_dir) if f.endswith('.py') and not f.startswith('__')])

    down_rev = None
    for f in files:
        if f < migration_id + ".py":
            down_rev = f[:-3]  # Remove .py

    content = f'''"""
{description}

This is a stub migration created to fix a mismatch between the database
and the migration files. The original migration was deleted from the codebase
but still exists in the database.

Revision ID: {migration_id}
Revises: {down_rev if down_rev else 'None'}
Create Date: {timestamp}
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "{migration_id}"
down_revision = "{down_rev if down_rev else 'None'}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This migration was previously removed from the codebase.
    # It should already be applied to the database, so nothing to do.
    pass


def downgrade() -> None:
    # Cannot downgrade - original migration code is lost
    raise NotImplementedError(
        "Cannot downgrade past migration {migration_id}. "
        "This migration was removed from the codebase. "
        "To fix this, manually remove the migration entry from alembic_version table."
    )
'''

    try:
        with open(filename, 'w') as f:
            f.write(content)
        print(f"Created stub migration: {filename}")
        return True
    except Exception as e:
        print(f"Error creating migration: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python fix_migration_mismatch.py <migration_id>")
        print("Example: python fix_migration_mismatch.py 0014_add_site_financial_context")
        sys.exit(1)

    migration_id = sys.argv[1]
    if create_stub_migration(migration_id):
        print(f"\nCreated stub for {migration_id}")
        print("You can now run migrations. After deployment, consider cleaning up the database.")
        sys.exit(0)
    else:
        sys.exit(1)
