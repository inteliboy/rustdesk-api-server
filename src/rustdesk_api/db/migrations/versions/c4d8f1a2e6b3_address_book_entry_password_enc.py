"""address book entry password_enc

Revision ID: c4d8f1a2e6b3
Revises: b7e2d4f6a8c1
Create Date: 2026-09-20 19:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4d8f1a2e6b3'
down_revision: Union[str, None] = 'b7e2d4f6a8c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('address_book_entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('password_enc', sa.String(length=1024), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('address_book_entries', schema=None) as batch_op:
        batch_op.drop_column('password_enc')
