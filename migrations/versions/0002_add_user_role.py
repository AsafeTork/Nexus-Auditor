"""add user role column

Revision ID: 0002_add_user_role
Revises: 0001_init
Create Date: 2026-04-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_add_user_role"
down_revision = "0001_init"
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
    # sqlite fallback
    if dialect == "sqlite":
        rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
        return any((r[1] == column) for r in rows)
    # generic best-effort
    try:
        rows = conn.execute(sa.text(f"SELECT * FROM {table} LIMIT 0")).keys()
        return column in rows
    except Exception:
        return False


def upgrade() -> None:
    conn = op.get_bind()
    if not _col_exists(conn, "users", "role"):
        op.add_column("users", sa.Column("role", sa.String(length=32), server_default="member"))

    # If an old schema had is_admin, try to map it to role (best effort).
    if _col_exists(conn, "users", "is_admin"):
        try:
            conn.execute(sa.text("UPDATE users SET role='admin' WHERE is_admin=1"))
        except Exception:
            pass


def downgrade() -> None:
    conn = op.get_bind()
    if _col_exists(conn, "users", "role"):
        op.drop_column("users", "role")

