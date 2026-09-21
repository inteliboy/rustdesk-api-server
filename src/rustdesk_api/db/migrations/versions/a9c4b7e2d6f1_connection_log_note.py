"""connection log guid and note

Revision ID: a9c4b7e2d6f1
Revises: e6b3a8d1c5f2
Create Date: 2026-09-21 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a9c4b7e2d6f1'
down_revision: Union[str, None] = 'e6b3a8d1c5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('connection_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('guid', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('note', sa.String(length=1000), nullable=True))
        batch_op.create_index(batch_op.f('ix_connection_logs_guid'), ['guid'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('connection_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_connection_logs_guid'))
        batch_op.drop_column('note')
        batch_op.drop_column('guid')
