"""Accessories, request-form questions, help article categories, turn-in sessions,
device value settings, repair labor, contracts, new-student alerts, password link

Revision ID: e6a93c1d2b74
Revises: c4d81a2e6f53
Create Date: 2026-11-10 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e6a93c1d2b74'
down_revision = 'c4d81a2e6f53'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('accessory_type',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('device_type', sa.String(length=40), nullable=True),
        sa.Column('fee', sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column('default_on', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('assignment_accessory',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('assignment_id', sa.Integer(), nullable=False),
        sa.Column('accessory_type_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('fee', sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column('returned_at', sa.DateTime(), nullable=True),
        sa.Column('missing_at', sa.DateTime(), nullable=True),
        sa.Column('incident_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['assignment_id'], ['assignment_history.id'], ),
        sa.ForeignKeyConstraint(['accessory_type_id'], ['accessory_type.id'], ),
        sa.ForeignKeyConstraint(['incident_id'], ['incident.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('assignment_accessory', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_assignment_accessory_assignment_id'), ['assignment_id'], unique=False)

    op.create_table('collection_session',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('box_size', sa.Integer(), nullable=False),
        sa.Column('returns_device', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.String(length=160), nullable=True),
        sa.ForeignKeyConstraint(['site_id'], ['site.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('collection_session', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_collection_session_site_id'), ['site_id'], unique=False)
    op.create_table('collection_scan',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('asset_tag', sa.String(length=120), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=True),
        sa.Column('person_name', sa.String(length=160), nullable=True),
        sa.Column('box', sa.Integer(), nullable=False),
        sa.Column('slot', sa.Integer(), nullable=False),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('scanned_at', sa.DateTime(), nullable=False),
        sa.Column('scanned_by', sa.String(length=160), nullable=True),
        sa.Column('handed_back_at', sa.DateTime(), nullable=True),
        sa.Column('handed_back_by', sa.String(length=160), nullable=True),
        sa.ForeignKeyConstraint(['session_id'], ['collection_session.id'], ),
        sa.ForeignKeyConstraint(['person_id'], ['person.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_id', 'asset_tag', name='uq_collection_scan_tag')
    )
    with op.batch_alter_table('collection_scan', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_collection_scan_session_id'), ['session_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_collection_scan_person_id'), ['person_id'], unique=False)

    op.create_table('asset_value_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('life_years', sa.JSON(), nullable=True),
        sa.Column('salvage_percent', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('labor_rate', sa.Numeric(precision=8, scale=2), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('contract',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=160), nullable=False),
        sa.Column('vendor', sa.String(length=160), nullable=True),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('seats', sa.Integer(), nullable=True),
        sa.Column('cost', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('billing', sa.String(length=20), nullable=True),
        sa.Column('reference', sa.String(length=120), nullable=True),
        sa.Column('starts_on', sa.Date(), nullable=True),
        sa.Column('ends_on', sa.Date(), nullable=True),
        sa.Column('auto_renews', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('notice_days', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('owner', sa.String(length=160), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['site_id'], ['site.id'], ),
        sa.PrimaryKeyConstraint('id')
    )

    with op.batch_alter_table('site', schema=None) as batch_op:
        batch_op.add_column(sa.Column('password_lookup_url', sa.String(length=500), nullable=True))
    with op.batch_alter_table('person', schema=None) as batch_op:
        batch_op.add_column(sa.Column('device_exempt_reason', sa.String(length=120), nullable=True))
    with op.batch_alter_table('ticket_category', schema=None) as batch_op:
        batch_op.add_column(sa.Column('questions', sa.JSON(), nullable=True))
    with op.batch_alter_table('help_article', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ticket_category_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_help_article_ticket_category_id', 'ticket_category', ['ticket_category_id'], ['id'])
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('answers', sa.JSON(), nullable=True))
    with op.batch_alter_table('repair', schema=None) as batch_op:
        batch_op.add_column(sa.Column('labor_minutes', sa.Integer(), nullable=False, server_default='0'))
    with op.batch_alter_table('overdue_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('new_student_alerts', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('new_student_since', sa.DateTime(), nullable=True))
    # Watch only students added from now on: everyone already here without a
    # device is a known backlog, not news.
    op.execute("UPDATE overdue_settings SET new_student_since = CURRENT_TIMESTAMP WHERE new_student_since IS NULL")
    op.execute("INSERT INTO overdue_settings (id, repair_max_days, alerts_enabled, alert_hour, new_student_alerts, new_student_since) "
               "SELECT 1, 14, true, 7, true, CURRENT_TIMESTAMP WHERE NOT EXISTS (SELECT 1 FROM overdue_settings WHERE id = 1)")


def downgrade():
    with op.batch_alter_table('overdue_settings', schema=None) as batch_op:
        batch_op.drop_column('new_student_since')
        batch_op.drop_column('new_student_alerts')
    with op.batch_alter_table('repair', schema=None) as batch_op:
        batch_op.drop_column('labor_minutes')
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_column('answers')
    with op.batch_alter_table('help_article', schema=None) as batch_op:
        batch_op.drop_constraint('fk_help_article_ticket_category_id', type_='foreignkey')
        batch_op.drop_column('ticket_category_id')
    with op.batch_alter_table('ticket_category', schema=None) as batch_op:
        batch_op.drop_column('questions')
    with op.batch_alter_table('person', schema=None) as batch_op:
        batch_op.drop_column('device_exempt_reason')
    with op.batch_alter_table('site', schema=None) as batch_op:
        batch_op.drop_column('password_lookup_url')
    op.drop_table('contract')
    op.drop_table('asset_value_settings')
    with op.batch_alter_table('collection_scan', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_collection_scan_person_id'))
        batch_op.drop_index(batch_op.f('ix_collection_scan_session_id'))
    op.drop_table('collection_scan')
    with op.batch_alter_table('collection_session', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_collection_session_site_id'))
    op.drop_table('collection_session')
    with op.batch_alter_table('assignment_accessory', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_assignment_accessory_assignment_id'))
    op.drop_table('assignment_accessory')
    op.drop_table('accessory_type')
