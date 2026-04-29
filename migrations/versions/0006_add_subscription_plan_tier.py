"""add subscription plan tier

Revision ID: 0006_add_subscription_plan_tier
Revises: 0005_add_perf_indexes
Create Date: 2026-04-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_add_subscription_plan_tier"
down_revision = "0005_add_perf_indexes"
branch_labels = None
depends_on = None


def _col_exists(conn, table: str, column: str) -> bool:
    dialect = conn.dialect.name
    if dialect == "postgresql":
        q = sa.text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = :t AND column_name = :c
            LIMIT 1
            """
        )
        return bool(conn.execute(q, {"t": table, "c": column}).fetchone())
    if dialect == "sqlite":
        rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
        return any((r[1] == column) for r in rows)
    try:
        rows = conn.execute(sa.text(f"SELECT * FROM {table} LIMIT 0")).keys()
        return column in rows
    except Exception:
        return False


def upgrade() -> None:
    conn = op.get_bind()
    if not _col_exists(conn, "subscriptions", "plan_tier"):
        op.add_column("subscriptions", sa.Column("plan_tier", sa.String(length=32), server_default="free"))


def downgrade() -> None:
    conn = op.get_bind()
    if _col_exists(conn, "subscriptions", "plan_tier"):
        op.drop_column("subscriptions", "plan_tier")

