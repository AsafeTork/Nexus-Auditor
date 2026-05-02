"""add financial learning tracking

Revision ID: 0015
Revises: 0014_add_site_financial_context
Create Date: 2025-05-02 16:45:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0015'
down_revision = '0014_add_site_financial_context'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('learning_stats', sa.Column('revenue_impact_predicted', sa.Float(), nullable=True, server_default='0.0'))
    op.add_column('learning_stats', sa.Column('revenue_impact_observed', sa.Float(), nullable=True, server_default='0.0'))
    op.add_column('learning_stats', sa.Column('prediction_error_pct', sa.Float(), nullable=True, server_default='0.0'))


def downgrade():
    op.drop_column('learning_stats', 'prediction_error_pct')
    op.drop_column('learning_stats', 'revenue_impact_observed')
    op.drop_column('learning_stats', 'revenue_impact_predicted')
