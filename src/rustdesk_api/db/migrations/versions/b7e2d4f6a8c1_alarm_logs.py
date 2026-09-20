"""alarm logs

Revision ID: b7e2d4f6a8c1
Revises: a3c9d5e7b1f2
Create Date: 2026-09-20 15:10:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e2d4f6a8c1'
down_revision: Union[str, None] = 'a3c9d5e7b1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('alarm_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=True),
    sa.Column('rustdesk_id', sa.String(length=64), nullable=False),
    sa.Column('alarm_type', sa.Integer(), nullable=True),
    sa.Column('conn_id', sa.String(length=32), nullable=True),
    sa.Column('from_ip', sa.String(length=64), nullable=True),
    sa.Column('peer_id', sa.String(length=64), nullable=True),
    sa.Column('peer_name', sa.String(length=255), nullable=True),
    sa.Column('conn_type', sa.String(length=32), nullable=True),
    sa.Column('message', sa.String(length=255), nullable=True),
    sa.Column('logged_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('nonce', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_alarm_logs_device_id_devices'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_alarm_logs')),
    sa.UniqueConstraint('nonce', name=op.f('uq_alarm_logs_nonce'))
    )
    with op.batch_alter_table('alarm_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alarm_logs_device_id'), ['device_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_alarm_logs_logged_at'), ['logged_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_alarm_logs_rustdesk_id'), ['rustdesk_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('alarm_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alarm_logs_rustdesk_id'))
        batch_op.drop_index(batch_op.f('ix_alarm_logs_logged_at'))
        batch_op.drop_index(batch_op.f('ix_alarm_logs_device_id'))

    op.drop_table('alarm_logs')
