"""Equipment fields and locations, spot checks, LibreNMS settings

Revision ID: 444fbfd9d139
Revises: 88607120cfe8
Create Date: 2026-10-12 14:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '444fbfd9d139'
down_revision = '88607120cfe8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('location',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('site_id', sa.Integer(), nullable=False),
    sa.Column('parent_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['location.id'], ),
    sa.ForeignKeyConstraint(['site_id'], ['site.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('location', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_location_site_id'), ['site_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_location_parent_id'), ['parent_id'], unique=False)

    op.create_table('spot_check',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('asset_tag', sa.String(length=120), nullable=False),
    sa.Column('expected_person_id', sa.Integer(), nullable=True),
    sa.Column('found_person_id', sa.Integer(), nullable=True),
    sa.Column('result', sa.String(length=20), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('site_id', sa.Integer(), nullable=True),
    sa.Column('checked_by', sa.String(length=160), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('resolved_by', sa.String(length=160), nullable=True),
    sa.ForeignKeyConstraint(['expected_person_id'], ['person.id'], ),
    sa.ForeignKeyConstraint(['found_person_id'], ['person.id'], ),
    sa.ForeignKeyConstraint(['site_id'], ['site.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('spot_check', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_spot_check_asset_tag'), ['asset_tag'], unique=False)
        batch_op.create_index(batch_op.f('ix_spot_check_result'), ['result'], unique=False)
        batch_op.create_index(batch_op.f('ix_spot_check_site_id'), ['site_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_spot_check_created_at'), ['created_at'], unique=False)

    op.create_table('librenms_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('url', sa.String(length=255), nullable=True),
    sa.Column('api_token', sa.Text(), nullable=True),
    sa.Column('verify_tls', sa.Boolean(), server_default=sa.true(), nullable=False),
    sa.Column('site_rules', sa.Text(), nullable=True),
    sa.Column('import_types', sa.JSON(), nullable=True),
    sa.Column('auto_add', sa.Boolean(), server_default=sa.true(), nullable=False),
    sa.Column('last_run_at', sa.DateTime(), nullable=True),
    sa.Column('last_summary', sa.JSON(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )

    with op.batch_alter_table('asset_registry', schema=None) as batch_op:
        batch_op.add_column(sa.Column('location_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('rack_position', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('hostname', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('ip_address', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('mac_address', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('firmware', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('management_url', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('support_ends', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('end_of_support', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('funding_source', sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column('erate_frn', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('funding_year', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_asset_registry_location_id'), ['location_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_asset_registry_hostname'), ['hostname'], unique=False)
        batch_op.create_foreign_key('fk_asset_registry_location_id', 'location', ['location_id'], ['id'])


def downgrade():
    with op.batch_alter_table('asset_registry', schema=None) as batch_op:
        batch_op.drop_constraint('fk_asset_registry_location_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_asset_registry_hostname'))
        batch_op.drop_index(batch_op.f('ix_asset_registry_location_id'))
        for column in ('funding_year', 'erate_frn', 'funding_source', 'end_of_support', 'support_ends', 'management_url',
                       'firmware', 'mac_address', 'ip_address', 'hostname', 'rack_position', 'location_id'):
            batch_op.drop_column(column)
    op.drop_table('librenms_settings')
    with op.batch_alter_table('spot_check', schema=None) as batch_op:
        for ix in ('ix_spot_check_created_at', 'ix_spot_check_site_id', 'ix_spot_check_result', 'ix_spot_check_asset_tag'):
            batch_op.drop_index(ix)
    op.drop_table('spot_check')
    with op.batch_alter_table('location', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_location_parent_id'))
        batch_op.drop_index(batch_op.f('ix_location_site_id'))
    op.drop_table('location')
