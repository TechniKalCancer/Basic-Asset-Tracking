"""Receipts for payments, and invoice/receipt settings

Revision ID: c4d81a2e6f53
Revises: b7c3e91f4a20
Create Date: 2026-10-22 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d81a2e6f53'
down_revision = 'b7c3e91f4a20'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('receipt',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=True),
        sa.Column('person_name', sa.String(length=160), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('method', sa.String(length=20), nullable=False),
        sa.Column('reference', sa.String(length=120), nullable=True),
        sa.Column('received_by', sa.String(length=160), nullable=True),
        sa.Column('items', sa.JSON(), nullable=False),
        sa.Column('paid_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['person_id'], ['person.id'], ),
        sa.ForeignKeyConstraint(['site_id'], ['site.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('receipt', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_receipt_person_id'), ['person_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_receipt_site_id'), ['site_id'], unique=False)

    op.create_table('invoice_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('payable_to', sa.String(length=160), nullable=True),
        sa.Column('pay_instructions', sa.Text(), nullable=True),
        sa.Column('contact', sa.Text(), nullable=True),
        sa.Column('footer', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )

    for table, column in (('incident', 'receipt_id'), ('ticket_charge', 'receipt_id'), ('agreement_signature', 'plan_receipt_id')):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column(column, sa.Integer(), nullable=True))
            batch_op.create_index(batch_op.f(f'ix_{table}_{column}'), [column], unique=False)
            batch_op.create_foreign_key(f'fk_{table}_{column}', 'receipt', [column], ['id'])


def downgrade():
    for table, column in (('agreement_signature', 'plan_receipt_id'), ('ticket_charge', 'receipt_id'), ('incident', 'receipt_id')):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(f'fk_{table}_{column}', type_='foreignkey')
            batch_op.drop_index(batch_op.f(f'ix_{table}_{column}'))
            batch_op.drop_column(column)
    op.drop_table('invoice_settings')
    with op.batch_alter_table('receipt', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_receipt_site_id'))
        batch_op.drop_index(batch_op.f('ix_receipt_person_id'))
    op.drop_table('receipt')
