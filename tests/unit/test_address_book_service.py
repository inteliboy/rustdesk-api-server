import pytest

from rustdesk_api.db.database import get_session_factory
from rustdesk_api.models.address_book_entry import AddressBookEntry, AddressBookTag
from rustdesk_api.services import address_book as ab
from rustdesk_api.services import authentication as auth_service


def _user(db, name):
    user = auth_service.create_user(db, username=name, password="passwordpassword1")
    db.flush()
    return user


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (0xFF112233, 0xFF112233),  # the client's own ARGB integer
        ("4278190335", 0xFF0000FF),  # numeric string
        ("#ff0000", 0xFFFF0000),  # WebUI style hex
        (0x112233, 0xFF112233),  # no alpha channel: made opaque
        (-1, 0xFFFFFFFF),  # a negative int wraps to 32 bits instead of overflowing
        (None, 0xFF64748B),
        ("red", 0xFF64748B),
        ({"nested": 1}, 0xFF64748B),
        ("#12345", 0xFF64748B),
    ],
)
def test_normalize_color(given, expected):
    assert ab.normalize_color(given) == expected


def test_color_hex_drops_alpha():
    assert AddressBookTag(color=0x80ABCDEF).color_hex == "#abcdef"


def test_personal_book_is_created_once(app):
    with get_session_factory()() as db:
        user = _user(db, "alice")
        first = ab.get_personal_book(db, user.id)
        assert ab.get_personal_book(db, user.id).id == first.id
        assert first.is_personal and first.name == "My address book"


def test_personal_book_creation_survives_a_concurrent_creator(app, monkeypatch):
    """If another request creates the book between our lookup and insert, the
    unique (owner, name) constraint fires and we must return theirs, not 500."""
    Session = get_session_factory()
    with Session() as db:
        user = _user(db, "alice")
        existing = ab.get_personal_book(db, user.id)
        db.commit()
        user_id, existing_id = user.id, existing.id

    with Session() as db:
        real_execute = db.execute
        calls = {"n": 0}

        def blind_first_lookup(stmt, *args, **kwargs):
            calls["n"] += 1
            result = real_execute(stmt, *args, **kwargs)
            if calls["n"] == 1:  # pretend the row did not exist yet
                return type("R", (), {"scalar_one_or_none": staticmethod(lambda: None)})()
            return result

        monkeypatch.setattr(db, "execute", blind_first_lookup)
        assert ab.get_personal_book(db, user_id).id == existing_id


def test_a_user_cannot_have_two_shared_books_with_the_same_name(app):
    with get_session_factory()() as db:
        user = _user(db, "alice")
        ab.create_shared_book(db, owner_id=user.id, name="Team")
        with pytest.raises(ab.Conflict):
            ab.create_shared_book(db, owner_id=user.id, name="  TEAM ")
        with pytest.raises(ab.Conflict):
            ab.create_shared_book(db, owner_id=user.id, name="my address book")
        with pytest.raises(ab.Invalid):
            ab.create_shared_book(db, owner_id=user.id, name="   ")


def test_visibility_and_rules(app):
    with get_session_factory()() as db:
        owner, reader, stranger = _user(db, "owner"), _user(db, "reader"), _user(db, "stranger")
        book = ab.create_shared_book(db, owner_id=owner.id, name="Team")
        ab.set_share(db, book, user_id=reader.id, rule=1)
        personal = ab.get_personal_book(db, owner.id)

        assert ab.rule_for(book, owner.id) == 3
        assert ab.rule_for(book, reader.id) == 1
        assert ab.rule_for(book, stranger.id) is None
        # A personal book has no shares, and is invisible to everyone else.
        assert ab.rule_for(personal, reader.id) is None
        assert ab.find_book(db, book.guid, stranger.id) is None
        assert ab.find_book(db, "no-such-guid", owner.id) is None
        assert [b.id for b, _ in ab.visible_shared_books(db, reader.id)] == [book.id]
        assert ab.can_write(1) is False and ab.can_write(2) and ab.can_write(3) and not ab.can_write(None)


def test_sharing_rejects_bad_input(app):
    with get_session_factory()() as db:
        owner, other = _user(db, "owner"), _user(db, "other")
        book = ab.create_shared_book(db, owner_id=owner.id, name="Team")
        for bad_rule in (0, 4, -1):
            with pytest.raises(ab.Invalid):
                ab.set_share(db, book, user_id=other.id, rule=bad_rule)
        with pytest.raises(ab.Invalid):
            ab.set_share(db, book, user_id=owner.id, rule=2)
        with pytest.raises(ab.Invalid):
            ab.set_share(db, ab.get_personal_book(db, owner.id), user_id=other.id, rule=1)


def test_client_names_are_unique_and_the_viewers_own_books_win(app):
    with get_session_factory()() as db:
        alice, bob = _user(db, "alice"), _user(db, "bob")
        theirs = ab.create_shared_book(db, owner_id=bob.id, name="Team")  # created first
        mine = ab.create_shared_book(db, owner_id=alice.id, name="Team")
        ab.set_share(db, theirs, user_id=alice.id, rule=1)

        names = ab.client_profile_names(ab.visible_shared_books(db, alice.id), alice.id)
        assert names[mine.id] == "Team"
        assert names[theirs.id] == "Team (bob)"


def test_tag_names_are_trimmed_bounded_and_deduplicated(app):
    with get_session_factory()() as db:
        user = _user(db, "alice")
        book = ab.get_personal_book(db, user.id)
        entry = ab.add_peer(db, book, {"id": "1", "tags": [" a ", "a", "", 5, None, "b" * 300]})
        assert sorted(t.name for t in entry.tags) == ["a", "b" * 100]


def test_deleting_a_tag_or_entry_cleans_up_links(app):
    with get_session_factory()() as db:
        user = _user(db, "alice")
        book = ab.get_personal_book(db, user.id)
        ab.add_peer(db, book, {"id": "1", "tags": ["x", "y"]})
        assert ab.delete_tags(db, book, ["x"]) == 1
        db.expire_all()
        assert [t.name for t in ab.get_entry_by_rustdesk_id(db, book.id, "1").tags] == ["y"]
        assert ab.delete_peers(db, book, ["1"]) == 1
        assert ab.list_entries(db, book.id) == []


def test_deleting_a_user_removes_their_books_entries_and_the_shares_they_gave(app):
    Session = get_session_factory()
    with Session() as db:
        owner, guest = _user(db, "owner"), _user(db, "guest")
        book = ab.create_shared_book(db, owner_id=owner.id, name="Team")
        ab.set_share(db, book, user_id=guest.id, rule=2)
        ab.add_peer(db, book, {"id": "1", "tags": ["t"]})
        ab.add_peer(db, ab.get_personal_book(db, owner.id), {"id": "2"})
        db.commit()
        owner_id, guest_id = owner.id, guest.id

    with Session() as db:
        db.delete(db.get(auth_service.User, owner_id))
        db.commit()
        assert ab.visible_shared_books(db, guest_id) == []
        assert db.query(AddressBookEntry).count() == 0
        assert db.query(AddressBookTag).count() == 0
