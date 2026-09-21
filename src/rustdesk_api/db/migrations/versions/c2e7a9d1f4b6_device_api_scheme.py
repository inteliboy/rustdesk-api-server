"""device api scheme and strategy sent time

Revision ID: c2e7a9d1f4b6
Revises: b8d3e5f7a9c2
Create Date: 2026-09-21 18:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2e7a9d1f4b6'
down_revision: Union[str, None] = 'b8d3e5f7a9c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('api_scheme', sa.String(length=5), nullable=True))
        batch_op.add_column(sa.Column('strategy_sent_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_column('strategy_sent_at')
        batch_op.drop_column('api_scheme')
