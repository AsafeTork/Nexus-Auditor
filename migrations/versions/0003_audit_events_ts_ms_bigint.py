"""audit_events.ts_ms bigint

Revision ID: 0003_audit_events_ts_ms_bigint
Revises: 0002_add_user_role
Create Date: 2026-04-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_audit_events_ts_ms_bigint"
down_revision = "0002_add_user_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres: alter type to BIGINT to support epoch ms
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE audit_events ALTER COLUMN ts_ms TYPE BIGINT")
    else:
        # SQLite / others: no-op (Integer already large enough in SQLite)
        pass


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE audit_events ALTER COLUMN ts_ms TYPE INTEGER")
    else:
        pass

