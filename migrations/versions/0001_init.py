"""init

Revision ID: 0001_init
Revises: 
Create Date: 2026-04-27
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orgs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_utc", sa.String(length=40)),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("org_id", sa.String(length=36), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("role", sa.String(length=32)),
        sa.Column("created_utc", sa.String(length=40)),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_org_id", "users", ["org_id"], unique=False)

    op.create_table(
        "sites",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("org_id", sa.String(length=36), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.String(length=1000), nullable=False),
        sa.Column("created_utc", sa.String(length=40)),
    )
    op.create_index("ix_sites_org_id", "sites", ["org_id"], unique=False)

    op.create_table(
        "audit_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("org_id", sa.String(length=36), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("site_id", sa.String(length=36), sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("created_utc", sa.String(length=40)),
        sa.Column("updated_utc", sa.String(length=40)),
        sa.Column("status", sa.String(length=32)),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("provider_base_url_v1", sa.String(length=1000), nullable=False),
        sa.Column("logs", sa.Text()),
        sa.Column("markdown_text", sa.Text()),
        sa.Column("csv_text", sa.Text()),
        sa.Column("target_domain", sa.String(length=255)),
    )
    op.create_index("ix_audit_runs_org_id", "audit_runs", ["org_id"], unique=False)
    op.create_index("ix_audit_runs_site_id", "audit_runs", ["site_id"], unique=False)

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("audit_run_id", sa.String(length=36), sa.ForeignKey("audit_runs.id"), nullable=False),
        sa.Column("ts_ms", sa.BigInteger()),
        sa.Column("layer", sa.String(length=200)),
        sa.Column("level", sa.String(length=16)),
        sa.Column("message", sa.Text()),
    )
    op.create_index("ix_audit_events_audit_run_id", "audit_events", ["audit_run_id"], unique=False)

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("org_id", sa.String(length=36), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("status", sa.String(length=32)),
        sa.Column("stripe_customer_id", sa.String(length=128)),
        sa.Column("stripe_subscription_id", sa.String(length=128)),
        sa.Column("created_utc", sa.String(length=40)),
        sa.Column("updated_utc", sa.String(length=40)),
    )
    op.create_index("ix_subscriptions_org_id", "subscriptions", ["org_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_subscriptions_org_id", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index("ix_audit_events_audit_run_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_audit_runs_site_id", table_name="audit_runs")
    op.drop_index("ix_audit_runs_org_id", table_name="audit_runs")
    op.drop_table("audit_runs")
    op.drop_index("ix_sites_org_id", table_name="sites")
    op.drop_table("sites")
    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.drop_table("orgs")
