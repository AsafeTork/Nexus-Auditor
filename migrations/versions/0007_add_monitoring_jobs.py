"""add continuous monitoring jobs

Revision ID: 0007_add_monitoring_jobs
Revises: 0006_add_subscription_plan_tier
Create Date: 2026-04-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007_add_monitoring_jobs"
down_revision = "0006_add_subscription_plan_tier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monitoring_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("org_id", sa.String(length=36), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("site_id", sa.String(length=36), sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("frequency_s", sa.Integer(), server_default="3600"),
        sa.Column("mode", sa.String(length=16), server_default="full"),
        sa.Column("scope_json", sa.Text()),
        sa.Column("safety_policy_ref", sa.String(length=200)),
        sa.Column("next_run_utc", sa.String(length=40)),
        sa.Column("last_run_utc", sa.String(length=40)),
        sa.Column("created_utc", sa.String(length=40)),
        sa.Column("updated_utc", sa.String(length=40)),
    )
    op.create_index("ix_monitoring_jobs_org_id", "monitoring_jobs", ["org_id"], unique=False)
    op.create_index("ix_monitoring_jobs_site_id", "monitoring_jobs", ["site_id"], unique=False)

    op.create_table(
        "monitoring_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("org_id", sa.String(length=36), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("site_id", sa.String(length=36), sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("job_id", sa.String(length=36), sa.ForeignKey("monitoring_jobs.id"), nullable=False),
        sa.Column("audit_run_id", sa.String(length=36), sa.ForeignKey("audit_runs.id"), nullable=False),
        sa.Column("status", sa.String(length=32)),
        sa.Column("findings_hash", sa.String(length=80)),
        sa.Column("findings_json", sa.Text()),
        sa.Column("diff_json", sa.Text()),
        sa.Column("created_utc", sa.String(length=40)),
    )
    op.create_index("ix_monitoring_runs_org_id", "monitoring_runs", ["org_id"], unique=False)
    op.create_index("ix_monitoring_runs_site_id", "monitoring_runs", ["site_id"], unique=False)
    op.create_index("ix_monitoring_runs_job_id", "monitoring_runs", ["job_id"], unique=False)
    op.create_index("ix_monitoring_runs_audit_run_id", "monitoring_runs", ["audit_run_id"], unique=False)

    # Add optional monitor_job_id link to audit_runs
    op.add_column("audit_runs", sa.Column("monitor_job_id", sa.String(length=36), nullable=True))
    op.create_index("ix_audit_runs_monitor_job_id", "audit_runs", ["monitor_job_id"], unique=False)


def downgrade() -> None:
    try:
        op.drop_index("ix_audit_runs_monitor_job_id", table_name="audit_runs")
        op.drop_column("audit_runs", "monitor_job_id")
    except Exception:
        pass

    op.drop_index("ix_monitoring_runs_audit_run_id", table_name="monitoring_runs")
    op.drop_index("ix_monitoring_runs_job_id", table_name="monitoring_runs")
    op.drop_index("ix_monitoring_runs_site_id", table_name="monitoring_runs")
    op.drop_index("ix_monitoring_runs_org_id", table_name="monitoring_runs")
    op.drop_table("monitoring_runs")

    op.drop_index("ix_monitoring_jobs_site_id", table_name="monitoring_jobs")
    op.drop_index("ix_monitoring_jobs_org_id", table_name="monitoring_jobs")
    op.drop_table("monitoring_jobs")

