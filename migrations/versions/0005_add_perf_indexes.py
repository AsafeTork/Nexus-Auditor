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
    # NOTE:
    # Some deployments may already have these indexes (manual creation or a partial migration run).
    # Use IF NOT EXISTS to make this migration idempotent.
    op.execute("CREATE INDEX IF NOT EXISTS ix_users_org_id ON users (org_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_sites_org_id ON sites (org_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_runs_org_id ON audit_runs (org_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_runs_status ON audit_runs (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_runs_created_utc ON audit_runs (created_utc)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_events_audit_run_id ON audit_events (audit_run_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_events_ts_ms ON audit_events (ts_ms)")


def downgrade():
    # Drop defensively.
    op.execute("DROP INDEX IF EXISTS ix_audit_events_ts_ms")
    op.execute("DROP INDEX IF EXISTS ix_audit_events_audit_run_id")
    op.execute("DROP INDEX IF EXISTS ix_audit_runs_created_utc")
    op.execute("DROP INDEX IF EXISTS ix_audit_runs_status")
    op.execute("DROP INDEX IF EXISTS ix_audit_runs_org_id")
    op.execute("DROP INDEX IF EXISTS ix_sites_org_id")
    op.execute("DROP INDEX IF EXISTS ix_users_org_id")
