"""Repair status and notes, site techs and overdue email settings, student schedules and bell times

Revision ID: b7c3e91f4a20
Revises: 5d1c0e7b9a42
Create Date: 2026-10-15 18:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7c3e91f4a20'
down_revision = '5d1c0e7b9a42'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('repair', schema=None) as batch_op:
        batch_op.add_column(sa.Column('status', sa.String(length=20), nullable=False, server_default='received'))
        batch_op.add_column(sa.Column('status_changed_at', sa.DateTime(), nullable=True))
    # An open repair with the vendor's RMA number filled in has gone out to the vendor.
    op.execute("UPDATE repair SET status = 'at_vendor' WHERE returned_at IS NULL AND ticket_number IS NOT NULL "
               "AND ticket_number <> ''")

    op.create_table('repair_note',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('repair_id', sa.Integer(), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('status_from', sa.String(length=20), nullable=True),
        sa.Column('status_to', sa.String(length=20), nullable=True),
        sa.Column('author_label', sa.String(length=120), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['repair_id'], ['repair.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('repair_note', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_repair_note_repair_id'), ['repair_id'], unique=False)

    op.create_table('site_tech',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['site_id'], ['site.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('site_id', 'user_id', name='uq_site_tech')
    )
    with op.batch_alter_table('site_tech', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_site_tech_site_id'), ['site_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_site_tech_user_id'), ['user_id'], unique=False)

    op.create_table('overdue_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('repair_max_days', sa.Integer(), nullable=False, server_default='14'),
        sa.Column('alerts_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('alert_hour', sa.Integer(), nullable=False, server_default='7'),
        sa.Column('last_alert_on', sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table('student_schedule',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.Column('period', sa.String(length=40), nullable=False),
        sa.Column('course', sa.String(length=160), nullable=True),
        sa.Column('teacher', sa.String(length=120), nullable=True),
        sa.Column('room', sa.String(length=40), nullable=True),
        sa.Column('term', sa.String(length=40), nullable=True),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['person_id'], ['person.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('student_schedule', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_student_schedule_person_id'), ['person_id'], unique=False)

    with op.batch_alter_table('site', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bell_schedule', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('site', schema=None) as batch_op:
        batch_op.drop_column('bell_schedule')
    with op.batch_alter_table('student_schedule', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_student_schedule_person_id'))
    op.drop_table('student_schedule')
    op.drop_table('overdue_settings')
    with op.batch_alter_table('site_tech', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_site_tech_user_id'))
        batch_op.drop_index(batch_op.f('ix_site_tech_site_id'))
    op.drop_table('site_tech')
    with op.batch_alter_table('repair_note', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_repair_note_repair_id'))
    op.drop_table('repair_note')
    with op.batch_alter_table('repair', schema=None) as batch_op:
        batch_op.drop_column('status_changed_at')
        batch_op.drop_column('status')
