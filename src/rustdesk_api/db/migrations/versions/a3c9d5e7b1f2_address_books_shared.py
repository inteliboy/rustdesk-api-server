"""address books: personal + shared books, per-book tags

Adds the newer RustDesk address-book model (`/api/ab/personal`,
`/api/ab/shared/profiles`, ...): entries belong to an `address_books` row
instead of directly to a user, tags become per-book with the client's ARGB
integer colors, and books can be shared with other users at a rule.

Existing data is kept: every user who has entries gets a personal book, their
entries move into it, and the tags their entries used are copied into that
book (the global `tags` table, still used by devices, is left untouched).

Revision ID: a3c9d5e7b1f2
Revises: 133f21b95239
Create Date: 2026-09-20 18:00:00.000000
"""
from __future__ import annotations

import datetime
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3c9d5e7b1f2'
down_revision: Union[str, None] = '133f21b95239'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PERSONAL_NAME = "My address book"
DEFAULT_COLOR = 0xFF64748B


def _argb(value: object) -> int:
    """Old tag colors were free-form strings: `#rrggbb` (WebUI-created) or a
    decimal ARGB integer (pushed by a client)."""
    text = str(value or "").strip()
    try:
        if text.startswith("#") and len(text) == 7:
            return 0xFF000000 | int(text[1:], 16)
        number = int(text) & 0xFFFFFFFF
    except ValueError:
        return DEFAULT_COLOR
    return number if number > 0xFFFFFF else 0xFF000000 | number


def _hex(argb: int) -> str:
    return f"#{argb & 0xFFFFFF:06x}"


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.datetime.now(datetime.timezone.utc)

    op.create_table(
        'address_books',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('guid', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('is_personal', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_address_books_owner_id_users'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_address_books')),
        sa.UniqueConstraint('guid', name=op.f('uq_address_books_guid')),
        sa.UniqueConstraint('owner_id', 'name', name='uq_address_book_owner_name'),
    )
    with op.batch_alter_table('address_books', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_address_books_owner_id'), ['owner_id'], unique=False)

    op.create_table(
        'address_book_shares',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('address_book_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('rule', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['address_book_id'], ['address_books.id'], name=op.f('fk_address_book_shares_address_book_id_address_books'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_address_book_shares_user_id_users'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_address_book_shares')),
        sa.UniqueConstraint('address_book_id', 'user_id', name='uq_ab_share_book_user'),
    )
    with op.batch_alter_table('address_book_shares', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_address_book_shares_address_book_id'), ['address_book_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_address_book_shares_user_id'), ['user_id'], unique=False)

    op.create_table(
        'address_book_tags',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('address_book_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('color', sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(['address_book_id'], ['address_books.id'], name=op.f('fk_address_book_tags_address_book_id_address_books'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_address_book_tags')),
        sa.UniqueConstraint('address_book_id', 'name', name='uq_ab_tag_book_name'),
    )
    with op.batch_alter_table('address_book_tags', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_address_book_tags_address_book_id'), ['address_book_id'], unique=False)

    # Read what has to move, then drop the old link table *before* rebuilding
    # `address_book_entries` (a table rebuild would otherwise fire its
    # ON DELETE CASCADE into the old link rows on databases with foreign keys
    # enforced).
    owners = [r[0] for r in bind.execute(sa.text("SELECT DISTINCT owner_id FROM address_book_entries"))]
    entry_owner = {r[0]: r[1] for r in bind.execute(sa.text("SELECT id, owner_id FROM address_book_entries"))}
    old_links = list(bind.execute(sa.text(
        "SELECT l.entry_id, t.name, t.color FROM address_book_entry_tags l JOIN tags t ON t.id = l.tag_id"
    )))
    op.drop_table('address_book_entry_tags')

    books = sa.table(
        'address_books',
        sa.column('id', sa.Integer), sa.column('guid', sa.String), sa.column('name', sa.String),
        sa.column('owner_id', sa.Integer), sa.column('is_personal', sa.Boolean),
        sa.column('created_at', sa.DateTime(timezone=True)), sa.column('updated_at', sa.DateTime(timezone=True)),
    )
    book_by_owner: dict[int, int] = {}
    for owner_id in owners:
        guid = str(uuid.uuid4())
        bind.execute(books.insert().values(
            guid=guid, name=PERSONAL_NAME, owner_id=owner_id, is_personal=True,
            created_at=now, updated_at=now,
        ))
        book_by_owner[owner_id] = int(
            bind.execute(sa.text("SELECT id FROM address_books WHERE guid = :g"), {"g": guid}).scalar_one()
        )

    with op.batch_alter_table('address_book_entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('address_book_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('note', sa.String(length=500), nullable=True))

    for owner_id, book_id in book_by_owner.items():
        bind.execute(
            sa.text("UPDATE address_book_entries SET address_book_id = :b WHERE owner_id = :o"),
            {"b": book_id, "o": owner_id},
        )

    tags = sa.table(
        'address_book_tags',
        sa.column('id', sa.Integer), sa.column('address_book_id', sa.Integer),
        sa.column('name', sa.String), sa.column('color', sa.BigInteger),
    )
    tag_ids: dict[tuple[int, str], int] = {}
    new_links: list[tuple[int, int]] = []
    for entry_id, name, color in old_links:
        book_id = book_by_owner[entry_owner[entry_id]]
        key = (book_id, name)
        if key not in tag_ids:
            bind.execute(tags.insert().values(address_book_id=book_id, name=name[:100], color=_argb(color)))
            tag_ids[key] = int(bind.execute(
                sa.text("SELECT id FROM address_book_tags WHERE address_book_id = :b AND name = :n"),
                {"b": book_id, "n": name[:100]},
            ).scalar_one())
        new_links.append((entry_id, tag_ids[key]))

    with op.batch_alter_table('address_book_entries', schema=None) as batch_op:
        batch_op.drop_constraint('uq_ab_entry_owner_rustdesk_id', type_='unique')
        batch_op.drop_index('ix_address_book_entries_owner_id')
        batch_op.drop_column('owner_id')
        batch_op.alter_column('address_book_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key(
            op.f('fk_address_book_entries_address_book_id_address_books'),
            'address_books', ['address_book_id'], ['id'], ondelete='CASCADE',
        )
        batch_op.create_unique_constraint('uq_ab_entry_book_rustdesk_id', ['address_book_id', 'rustdesk_id'])
        batch_op.create_index(batch_op.f('ix_address_book_entries_address_book_id'), ['address_book_id'], unique=False)

    link = op.create_table(
        'address_book_entry_tags',
        sa.Column('entry_id', sa.Integer(), nullable=False),
        sa.Column('tag_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['entry_id'], ['address_book_entries.id'], name=op.f('fk_address_book_entry_tags_entry_id_address_book_entries'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tag_id'], ['address_book_tags.id'], name=op.f('fk_address_book_entry_tags_tag_id_address_book_tags'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('entry_id', 'tag_id', name=op.f('pk_address_book_entry_tags')),
    )
    if new_links:
        op.bulk_insert(link, [{"entry_id": e, "tag_id": t} for e, t in new_links])


def downgrade() -> None:
    """Best effort: entries of shared books have nowhere to go in the old
    single-owner schema and are dropped; per-book tags are folded back into
    the global `tags` table (an existing tag keeps its color)."""
    bind = op.get_bind()

    entry_owner = {
        r[0]: r[1]
        for r in bind.execute(sa.text(
            "SELECT e.id, b.owner_id FROM address_book_entries e "
            "JOIN address_books b ON b.id = e.address_book_id WHERE b.is_personal = 1"
        ))
    }
    old_links = list(bind.execute(sa.text(
        "SELECT l.entry_id, t.name, t.color FROM address_book_entry_tags l "
        "JOIN address_book_tags t ON t.id = l.tag_id"
    )))
    op.drop_table('address_book_entry_tags')

    with op.batch_alter_table('address_book_entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('owner_id', sa.Integer(), nullable=True))
    for entry_id, owner_id in entry_owner.items():
        bind.execute(sa.text("UPDATE address_book_entries SET owner_id = :o WHERE id = :i"), {"o": owner_id, "i": entry_id})
    bind.execute(sa.text("DELETE FROM address_book_entries WHERE owner_id IS NULL"))

    with op.batch_alter_table('address_book_entries', schema=None) as batch_op:
        batch_op.drop_index('ix_address_book_entries_address_book_id')
        batch_op.drop_constraint('uq_ab_entry_book_rustdesk_id', type_='unique')
        batch_op.drop_constraint('fk_address_book_entries_address_book_id_address_books', type_='foreignkey')
        batch_op.drop_column('address_book_id')
        batch_op.drop_column('note')
        batch_op.alter_column('owner_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key(
            op.f('fk_address_book_entries_owner_id_users'), 'users', ['owner_id'], ['id'], ondelete='CASCADE'
        )
        batch_op.create_unique_constraint('uq_ab_entry_owner_rustdesk_id', ['owner_id', 'rustdesk_id'])
        batch_op.create_index(batch_op.f('ix_address_book_entries_owner_id'), ['owner_id'], unique=False)

    global_tags = {r[0]: r[1] for r in bind.execute(sa.text("SELECT name, id FROM tags"))}
    tags = sa.table('tags', sa.column('id', sa.Integer), sa.column('name', sa.String), sa.column('color', sa.String))
    link_rows = []
    for entry_id, name, color in old_links:
        if entry_id not in entry_owner:
            continue
        if name not in global_tags:
            bind.execute(tags.insert().values(name=name[:50], color=_hex(int(color))))
            global_tags[name] = int(
                bind.execute(sa.text("SELECT id FROM tags WHERE name = :n"), {"n": name[:50]}).scalar_one()
            )
        link_rows.append({"entry_id": entry_id, "tag_id": global_tags[name]})

    link = op.create_table(
        'address_book_entry_tags',
        sa.Column('entry_id', sa.Integer(), nullable=False),
        sa.Column('tag_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['entry_id'], ['address_book_entries.id'], name=op.f('fk_address_book_entry_tags_entry_id_address_book_entries'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], name=op.f('fk_address_book_entry_tags_tag_id_tags'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('entry_id', 'tag_id', name=op.f('pk_address_book_entry_tags')),
    )
    if link_rows:
        op.bulk_insert(link, link_rows)

    op.drop_table('address_book_tags')
    op.drop_table('address_book_shares')
    op.drop_table('address_books')
