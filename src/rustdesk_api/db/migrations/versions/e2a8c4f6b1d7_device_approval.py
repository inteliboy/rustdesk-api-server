"""device approval: a new device can wait for an administrator

Revision ID: e2a8c4f6b1d7
Revises: d9f3a1b7c5e8
Create Date: 2026-09-21 21:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2a8c4f6b1d7'
down_revision: Union[str, None] = 'd9f3a1b7c5e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Every device that exists now is approved (the server default fills the column).
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('approval', sa.String(length=10), server_default='approved', nullable=False))
        batch_op.create_index(batch_op.f('ix_devices_approval'), ['approval'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_devices_approval'))
        batch_op.drop_column('approval')
