"""add learning store

Revision ID: 0010_add_learning_store
Revises: 0009_add_verification_loop
Create Date: 2026-04-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0010_add_learning_store"
down_revision = "0009_add_verification_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("learning_stats"):
        op.create_table(
            "learning_stats",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("org_id", sa.String(length=36), sa.ForeignKey("orgs.id"), nullable=False),
            sa.Column("job_id", sa.String(length=36), sa.ForeignKey("monitoring_jobs.id"), nullable=False),
            sa.Column("finding_key", sa.String(length=500), nullable=False),
            sa.Column("rec_kind", sa.String(length=64), server_default="unknown"),
            sa.Column("seen_count", sa.Integer(), server_default="0"),
            sa.Column("resolved_count", sa.Integer(), server_default="0"),
            sa.Column("open_count", sa.Integer(), server_default="0"),
            sa.Column("regression_count", sa.Integer(), server_default="0"),
            sa.Column("avg_resolution_s", sa.Integer(), server_default="0"),
            sa.Column("created_utc", sa.String(length=40)),
            sa.Column("updated_utc", sa.String(length=40)),
        )
        insp = sa.inspect(bind)
    idx = {ix["name"] for ix in insp.get_indexes("learning_stats")} if insp.has_table("learning_stats") else set()
    if "ix_learning_stats_org_id" not in idx:
        op.create_index("ix_learning_stats_org_id", "learning_stats", ["org_id"], unique=False)
    if "ix_learning_stats_job_id" not in idx:
        op.create_index("ix_learning_stats_job_id", "learning_stats", ["job_id"], unique=False)
    if "ix_learning_stats_finding_key" not in idx:
        op.create_index("ix_learning_stats_finding_key", "learning_stats", ["finding_key"], unique=False)
    if "ix_learning_stats_rec_kind" not in idx:
        op.create_index("ix_learning_stats_rec_kind", "learning_stats", ["rec_kind"], unique=False)


def downgrade() -> None:
    try:
        op.drop_index("ix_learning_stats_rec_kind", table_name="learning_stats")
        op.drop_index("ix_learning_stats_finding_key", table_name="learning_stats")
        op.drop_index("ix_learning_stats_job_id", table_name="learning_stats")
        op.drop_index("ix_learning_stats_org_id", table_name="learning_stats")
        op.drop_table("learning_stats")
    except Exception:
        pass
