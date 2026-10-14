"""Short names for agreements (the People list columns)

Revision ID: 5d1c0e7b9a42
Revises: ac46773f2ae5
Create Date: 2026-10-14 14:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5d1c0e7b9a42'
down_revision = 'ac46773f2ae5'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('agreement', schema=None) as batch_op:
        batch_op.add_column(sa.Column('short_name', sa.String(length=20), nullable=True))


def downgrade():
    with op.batch_alter_table('agreement', schema=None) as batch_op:
        batch_op.drop_column('short_name')
