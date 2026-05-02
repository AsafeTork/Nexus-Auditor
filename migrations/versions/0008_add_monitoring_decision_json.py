"""add monitoring decision json

Revision ID: 0008_add_monitoring_decision_json
Revises: 0007_add_monitoring_jobs
Create Date: 2026-04-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0008_add_monitoring_decision_json"
down_revision = "0007_add_monitoring_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("monitoring_runs")}
    if "decision_json" not in cols:
        op.add_column("monitoring_runs", sa.Column("decision_json", sa.Text(), server_default=""))


def downgrade() -> None:
    try:
        op.drop_column("monitoring_runs", "decision_json")
    except Exception:
        pass
