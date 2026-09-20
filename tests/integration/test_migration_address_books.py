"""The address-book migration must carry existing entries and tags across
(and back), since users' synced address books predate shared books."""

from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, text

from rustdesk_api.db.migrations.runner import get_alembic_config

PREVIOUS = "133f21b95239"
NOW = "2026-09-01 00:00:00"


def _seed_old_schema(engine) -> None:
    with engine.begin() as c:
        for uid, name in ((1, "alice"), (2, "bob")):
            c.execute(
                text(
                    "INSERT INTO users"
                    " (id, username, password_hash, is_active, is_admin, created_at, updated_at)"
                    " VALUES (:i, :n, 'x', 1, 0, :t, :t)"
                ),
                {"i": uid, "n": name, "t": NOW},
            )
        c.execute(
            text(
                "INSERT INTO tags (id, name, color)"
                " VALUES (1, 'office', '#ff0000'), (2, 'home', '4278190335')"
            )
        )
        for eid, owner, rid in ((1, 1, "111"), (2, 1, "222"), (3, 2, "111")):
            c.execute(
                text(
                    "INSERT INTO address_book_entries"
                    " (id, owner_id, rustdesk_id, alias, hash, created_at, updated_at)"
                    " VALUES (:i, :o, :r, :a, 'hashval', :t, :t)"
                ),
                {"i": eid, "o": owner, "r": rid, "a": f"alias{eid}", "t": NOW},
            )
        c.execute(
            text("INSERT INTO address_book_entry_tags (entry_id, tag_id) VALUES (1, 1), (1, 2), (3, 1)")
        )


def test_upgrade_moves_entries_and_tags_into_personal_books_and_downgrade_reverses(settings):
    cfg = get_alembic_config(settings)
    command.upgrade(cfg, PREVIOUS)
    engine = create_engine(settings.database_url)
    _seed_old_schema(engine)

    command.upgrade(cfg, "head")
    with engine.connect() as c:
        books = c.execute(
            text("SELECT owner_id, name, is_personal FROM address_books ORDER BY owner_id")
        ).all()
        assert [(b[0], b[1], bool(b[2])) for b in books] == [
            (1, "My address book", True),
            (2, "My address book", True),
        ]

        rows = c.execute(
            text(
                "SELECT e.rustdesk_id, b.owner_id, e.hash FROM address_book_entries e"
                " JOIN address_books b ON b.id = e.address_book_id ORDER BY e.id"
            )
        ).all()
        assert [tuple(r) for r in rows] == [
            ("111", 1, "hashval"),
            ("222", 1, "hashval"),
            ("111", 2, "hashval"),
        ]

        tags = c.execute(
            text(
                "SELECT b.owner_id, t.name, t.color FROM address_book_tags t"
                " JOIN address_books b ON b.id = t.address_book_id ORDER BY b.owner_id, t.name"
            )
        ).all()
        # '#ff0000' becomes opaque ARGB; a client-pushed decimal int is kept.
        assert [tuple(t) for t in tags] == [
            (1, "home", 4278190335),
            (1, "office", 0xFFFF0000),
            (2, "office", 0xFFFF0000),
        ]

        links = c.execute(
            text(
                "SELECT l.entry_id, t.name FROM address_book_entry_tags l"
                " JOIN address_book_tags t ON t.id = l.tag_id ORDER BY l.entry_id, t.name"
            )
        ).all()
        assert [tuple(x) for x in links] == [(1, "home"), (1, "office"), (3, "office")]

        # The global (device) tag vocabulary is untouched.
        assert c.execute(text("SELECT COUNT(*) FROM tags")).scalar_one() == 2

    command.downgrade(cfg, PREVIOUS)
    with engine.connect() as c:
        rows = c.execute(text("SELECT owner_id, rustdesk_id FROM address_book_entries ORDER BY id")).all()
        assert [tuple(r) for r in rows] == [(1, "111"), (1, "222"), (2, "111")]
        links = c.execute(
            text(
                "SELECT l.entry_id, t.name FROM address_book_entry_tags l"
                " JOIN tags t ON t.id = l.tag_id ORDER BY l.entry_id, t.name"
            )
        ).all()
        assert [tuple(x) for x in links] == [(1, "home"), (1, "office"), (3, "office")]
    engine.dispose()


def test_upgrade_on_an_empty_database_creates_no_books(settings):
    cfg = get_alembic_config(settings)
    command.upgrade(cfg, "head")
    engine = create_engine(settings.database_url)
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM address_books")).scalar_one() == 0
    engine.dispose()
