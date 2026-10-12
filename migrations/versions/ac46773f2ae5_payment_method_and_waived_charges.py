"""How a charge was paid, and waived charges

Revision ID: ac46773f2ae5
Revises: 444fbfd9d139
Create Date: 2026-10-12 18:40:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ac46773f2ae5'
down_revision = '444fbfd9d139'
branch_labels = None
depends_on = None

COLUMNS = [('paid_method', sa.String(length=20)), ('paid_note', sa.String(length=120)), ('waived_at', sa.DateTime()),
           ('waived_by', sa.String(length=160)), ('waive_reason', sa.String(length=255))]


def upgrade():
    for table in ('incident', 'ticket_charge'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name, kind in COLUMNS:
                batch_op.add_column(sa.Column(name, kind, nullable=True))
    with op.batch_alter_table('agreement_signature', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plan_waived_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('agreement_signature', schema=None) as batch_op:
        batch_op.drop_column('plan_waived_at')
    for table in ('ticket_charge', 'incident'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name, _ in reversed(COLUMNS):
                batch_op.drop_column(name)
