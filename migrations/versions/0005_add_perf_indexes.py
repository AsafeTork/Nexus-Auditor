"""add perf indexes

Revision ID: 0005_add_perf_indexes
Revises: 0004_add_org_llm_defaults
Create Date: 2026-04-28
"""

from alembic import op


revision = "0005_add_perf_indexes"
down_revision = "0004_add_org_llm_defaults"
branch_labels = None
depends_on = None


def upgrade():
    # Multi-tenant + listing performance
    op.create_index("ix_users_org_id", "users", ["org_id"])
    op.create_index("ix_sites_org_id", "sites", ["org_id"])
    op.create_index("ix_audit_runs_org_id", "audit_runs", ["org_id"])
    op.create_index("ix_audit_runs_status", "audit_runs", ["status"])
    op.create_index("ix_audit_runs_created_utc", "audit_runs", ["created_utc"])
    # Audit events lookup + retention delete
    op.create_index("ix_audit_events_audit_run_id", "audit_events", ["audit_run_id"])
    op.create_index("ix_audit_events_ts_ms", "audit_events", ["ts_ms"])


def downgrade():
    op.drop_index("ix_audit_events_ts_ms", table_name="audit_events")
    op.drop_index("ix_audit_events_audit_run_id", table_name="audit_events")
    op.drop_index("ix_audit_runs_created_utc", table_name="audit_runs")
    op.drop_index("ix_audit_runs_status", table_name="audit_runs")
    op.drop_index("ix_audit_runs_org_id", table_name="audit_runs")
    op.drop_index("ix_sites_org_id", table_name="sites")
    op.drop_index("ix_users_org_id", table_name="users")

