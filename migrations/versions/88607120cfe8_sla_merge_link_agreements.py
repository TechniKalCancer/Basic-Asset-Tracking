"""SLA targets, merged/linked tickets, device agreements, plan payments

Revision ID: 88607120cfe8
Revises: b29ba2c34e9b
Create Date: 2026-10-11 19:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '88607120cfe8'
down_revision = 'b29ba2c34e9b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('sla_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('work_days', sa.String(length=20), nullable=False),
    sa.Column('day_start', sa.String(length=5), nullable=False),
    sa.Column('day_end', sa.String(length=5), nullable=False),
    sa.Column('closed_dates', sa.Text(), nullable=True),
    sa.Column('targets', sa.JSON(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('agreement',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=160), nullable=False),
    sa.Column('school_year', sa.String(length=20), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('site_id', sa.Integer(), nullable=True),
    sa.Column('require_student', sa.Boolean(), nullable=False),
    sa.Column('require_guardian', sa.Boolean(), nullable=False),
    sa.Column('plan_offered', sa.Boolean(), nullable=False),
    sa.Column('plan_name', sa.String(length=120), nullable=True),
    sa.Column('plan_price', sa.Numeric(precision=8, scale=2), nullable=True),
    sa.Column('plan_details', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('last_reminded_at', sa.DateTime(), nullable=True),
    sa.Column('created_by', sa.String(length=160), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['site_id'], ['site.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('agreement', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_agreement_is_active'), ['is_active'], unique=False)

    op.create_table('agreement_signature',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('agreement_id', sa.Integer(), nullable=False),
    sa.Column('person_id', sa.Integer(), nullable=False),
    sa.Column('signer_role', sa.String(length=20), nullable=False),
    sa.Column('signer_name', sa.String(length=160), nullable=False),
    sa.Column('signer_email', sa.String(length=255), nullable=True),
    sa.Column('signed_at', sa.DateTime(), nullable=False),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.String(length=255), nullable=True),
    sa.Column('body_sha256', sa.String(length=64), nullable=False),
    sa.Column('plan_choice', sa.String(length=10), nullable=True),
    sa.Column('plan_amount', sa.Numeric(precision=8, scale=2), nullable=True),
    sa.Column('plan_paid_at', sa.DateTime(), nullable=True),
    sa.Column('recorded_by', sa.String(length=160), nullable=True),
    sa.ForeignKeyConstraint(['agreement_id'], ['agreement.id'], ),
    sa.ForeignKeyConstraint(['person_id'], ['person.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('agreement_id', 'person_id', 'signer_role', name='uq_agreement_signature_role')
    )
    with op.batch_alter_table('agreement_signature', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_agreement_signature_agreement_id'), ['agreement_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_agreement_signature_person_id'), ['person_id'], unique=False)

    with op.batch_alter_table('fee_payment', schema=None) as batch_op:
        batch_op.alter_column('incident_id', existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column('signature_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_fee_payment_signature_id'), ['signature_id'], unique=False)
        batch_op.create_foreign_key('fk_fee_payment_signature_id', 'agreement_signature', ['signature_id'], ['id'])

    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('first_response_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('response_due_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('resolve_due_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('merged_into_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('parent_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_ticket_response_due_at'), ['response_due_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_ticket_resolve_due_at'), ['resolve_due_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_ticket_parent_id'), ['parent_id'], unique=False)
        batch_op.create_foreign_key('fk_ticket_merged_into_id', 'ticket', ['merged_into_id'], ['id'])
        batch_op.create_foreign_key('fk_ticket_parent_id', 'ticket', ['parent_id'], ['id'])

    _backfill_first_responses()


def _backfill_first_responses():
    """A ticket's first response is the first reply its requester was sent,
    so the help desk reports have history from day one."""
    ticket = sa.table('ticket', sa.column('id', sa.Integer), sa.column('first_response_at', sa.DateTime))
    comment = sa.table('ticket_comment', sa.column('ticket_id', sa.Integer), sa.column('created_at', sa.DateTime),
                       sa.column('emailed_to_requester', sa.Boolean), sa.column('from_requester', sa.Boolean))
    first = (sa.select(sa.func.min(comment.c.created_at))
             .where(comment.c.ticket_id == ticket.c.id, comment.c.emailed_to_requester.is_(sa.true()),
                    comment.c.from_requester.is_(sa.false()))
             .scalar_subquery())
    op.get_bind().execute(sa.update(ticket).values(first_response_at=first))


def downgrade():
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_constraint('fk_ticket_parent_id', type_='foreignkey')
        batch_op.drop_constraint('fk_ticket_merged_into_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_ticket_parent_id'))
        batch_op.drop_index(batch_op.f('ix_ticket_resolve_due_at'))
        batch_op.drop_index(batch_op.f('ix_ticket_response_due_at'))
        batch_op.drop_column('parent_id')
        batch_op.drop_column('merged_into_id')
        batch_op.drop_column('resolve_due_at')
        batch_op.drop_column('response_due_at')
        batch_op.drop_column('first_response_at')

    with op.batch_alter_table('fee_payment', schema=None) as batch_op:
        batch_op.drop_constraint('fk_fee_payment_signature_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_fee_payment_signature_id'))
        batch_op.drop_column('signature_id')
        batch_op.alter_column('incident_id', existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table('agreement_signature', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_agreement_signature_person_id'))
        batch_op.drop_index(batch_op.f('ix_agreement_signature_agreement_id'))
    op.drop_table('agreement_signature')
    with op.batch_alter_table('agreement', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_agreement_is_active'))
    op.drop_table('agreement')
    op.drop_table('sla_settings')
