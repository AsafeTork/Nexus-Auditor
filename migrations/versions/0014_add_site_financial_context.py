"""add site financial context

Revision ID: 0014
Revises: 0013_add_org_llm_provider_and_key
Create Date: 2025-05-02 16:30:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0014'
down_revision = '0013_add_org_llm_provider_and_key'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sites', sa.Column('aov', sa.Float(), nullable=True, server_default='150.0'))
    op.add_column('sites', sa.Column('monthly_sessions', sa.Integer(), nullable=True, server_default='50000'))
    op.add_column('sites', sa.Column('conversion_rate', sa.Float(), nullable=True, server_default='0.025'))


def downgrade():
    op.drop_column('sites', 'conversion_rate')
    op.drop_column('sites', 'monthly_sessions')
    op.drop_column('sites', 'aov')
