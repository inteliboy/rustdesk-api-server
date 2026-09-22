"""ldap identities

Revision ID: bc90ceb4b99c
Revises: c4ebe4c8391a
Create Date: 2026-09-22 09:05:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'bc90ceb4b99c'
down_revision: Union[str, None] = 'c4ebe4c8391a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ldap_identities',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('ldap_username', sa.String(length=255), nullable=False),
        sa.Column('distinguished_name', sa.String(length=500), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'], name=op.f('fk_ldap_identities_user_id_users'), ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_ldap_identities')),
        sa.UniqueConstraint('user_id', name=op.f('uq_ldap_identities_user_id')),
    )
    with op.batch_alter_table('ldap_identities', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ldap_identities_ldap_username'), ['ldap_username'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('ldap_identities', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ldap_identities_ldap_username'))
    op.drop_table('ldap_identities')
