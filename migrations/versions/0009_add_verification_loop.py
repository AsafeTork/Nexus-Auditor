"""add verification loop

Revision ID: 0009_add_verification_loop
Revises: 0008_monitoring_decision
Create Date: 2026-04-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_add_verification_loop"
down_revision = "0008_monitoring_decision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("monitoring_runs", sa.Column("verification_json", sa.Text(), server_default=""))

    op.create_table(
        "monitoring_findings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("org_id", sa.String(length=36), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("site_id", sa.String(length=36), sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("job_id", sa.String(length=36), sa.ForeignKey("monitoring_jobs.id"), nullable=False),
        sa.Column("finding_key", sa.String(length=500), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="NEW"),
        sa.Column("first_seen_utc", sa.String(length=40)),
        sa.Column("last_seen_utc", sa.String(length=40)),
        sa.Column("resolved_utc", sa.String(length=40)),
        sa.Column("reopen_count", sa.Integer(), server_default="0"),
        sa.Column("regression_count", sa.Integer(), server_default="0"),
        sa.Column("resolution_time_s", sa.Integer(), server_default="0"),
        sa.Column("last_recommendation", sa.Text()),
        sa.Column("last_decision_run_id", sa.String(length=36)),
        sa.Column("created_utc", sa.String(length=40)),
        sa.Column("updated_utc", sa.String(length=40)),
    )
    op.create_index("ix_monitoring_findings_org_id", "monitoring_findings", ["org_id"], unique=False)
    op.create_index("ix_monitoring_findings_site_id", "monitoring_findings", ["site_id"], unique=False)
    op.create_index("ix_monitoring_findings_job_id", "monitoring_findings", ["job_id"], unique=False)
    op.create_index("ix_monitoring_findings_finding_key", "monitoring_findings", ["finding_key"], unique=False)


def downgrade() -> None:
    try:
        op.drop_index("ix_monitoring_findings_finding_key", table_name="monitoring_findings")
        op.drop_index("ix_monitoring_findings_job_id", table_name="monitoring_findings")
        op.drop_index("ix_monitoring_findings_site_id", table_name="monitoring_findings")
        op.drop_index("ix_monitoring_findings_org_id", table_name="monitoring_findings")
        op.drop_table("monitoring_findings")
    except Exception:
        pass
    try:
        op.drop_column("monitoring_runs", "verification_json")
    except Exception:
        pass
