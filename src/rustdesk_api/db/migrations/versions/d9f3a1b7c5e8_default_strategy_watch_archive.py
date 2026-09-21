"""default strategy, offline watch and archived devices

Revision ID: d9f3a1b7c5e8
Revises: c2e7a9d1f4b6
Create Date: 2026-09-21 20:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd9f3a1b7c5e8'
down_revision: Union[str, None] = 'c2e7a9d1f4b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('strategies', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_default', sa.Boolean(), server_default='0', nullable=False))

    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('watch_offline', sa.Boolean(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('offline_notified_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f('ix_devices_archived_at'), ['archived_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_devices_archived_at'))
        batch_op.drop_column('archived_at')
        batch_op.drop_column('offline_notified_at')
        batch_op.drop_column('watch_offline')

    with op.batch_alter_table('strategies', schema=None) as batch_op:
        batch_op.drop_column('is_default')
