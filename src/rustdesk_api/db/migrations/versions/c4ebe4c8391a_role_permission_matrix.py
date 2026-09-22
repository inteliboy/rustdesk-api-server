"""role permission matrix: roles, user groups, direct role assignment

Revision ID: c4ebe4c8391a
Revises: e2a8c4f6b1d7
Create Date: 2026-09-22 09:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4ebe4c8391a'
down_revision: Union[str, None] = 'e2a8c4f6b1d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'roles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('permissions_json', sa.Text(), nullable=False),
        sa.Column('requires_2fa', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_roles')),
    )
    with op.batch_alter_table('roles', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_roles_name'), ['name'], unique=True)

    op.create_table(
        'user_groups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('role_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['role_id'], ['roles.id'], name=op.f('fk_user_groups_role_id_roles'), ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_user_groups')),
    )
    with op.batch_alter_table('user_groups', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_groups_name'), ['name'], unique=True)
        batch_op.create_index(batch_op.f('ix_user_groups_role_id'), ['role_id'], unique=False)

    op.create_table(
        'user_group_members',
        sa.Column('user_group_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ['user_group_id'],
            ['user_groups.id'],
            name=op.f('fk_user_group_members_user_group_id_user_groups'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'], name=op.f('fk_user_group_members_user_id_users'), ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('user_group_id', 'user_id', name=op.f('pk_user_group_members')),
    )

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('role_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_role_id'), ['role_id'], unique=False)
        batch_op.create_foreign_key(
            batch_op.f('fk_users_role_id_roles'), 'roles', ['role_id'], ['id'], ondelete='SET NULL'
        )


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_users_role_id_roles'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_users_role_id'))
        batch_op.drop_column('role_id')

    op.drop_table('user_group_members')

    with op.batch_alter_table('user_groups', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_groups_role_id'))
        batch_op.drop_index(batch_op.f('ix_user_groups_name'))
    op.drop_table('user_groups')

    with op.batch_alter_table('roles', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_roles_name'))
    op.drop_table('roles')
