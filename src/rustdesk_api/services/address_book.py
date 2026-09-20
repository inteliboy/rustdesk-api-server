"""Address books: a personal book per user plus shared books, their peers
(entries), tags and shares.

Access model (the same for the RustDesk client protocol and the WebUI API):

- the owner of a book has full control of it;
- another user sees a book only through an `AddressBookShare`, whose `rule`
  is read (1), read/write (2) or full control (3);
- a personal book is never shared, so only its owner can see it.

Callers ask `find_book()` for a book by guid *as a given user*; it returns
nothing (rather than "forbidden") for a book the user has no rule on, so an
existing guid is never confirmed to somebody who may not see it.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from rustdesk_api.models.address_book_entry import (
    DEFAULT_TAG_COLOR,
    PERSONAL_BOOK_NAME,
    RULE_FULL_CONTROL,
    RULE_READ_WRITE,
    VALID_RULES,
    AddressBook,
    AddressBookEntry,
    AddressBookShare,
    AddressBookTag,
)
from rustdesk_api.security.encryption import SecretBox

logger = logging.getLogger(__name__)

MAX_ID_LENGTH = 64
MAX_TAG_NAME_LENGTH = 100
# RustDesk connection passwords are short; this only bounds the ciphertext column.
MAX_PASSWORD_LENGTH = 256


class AddressBookError(Exception):
    """Base for expected, user-facing failures. `code` is stable for API
    clients; `message` is safe to show."""

    code = "ADDRESS_BOOK_ERROR"
    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(AddressBookError):
    code = "NOT_FOUND"
    status = 404


class Forbidden(AddressBookError):
    code = "FORBIDDEN"
    status = 403


class Invalid(AddressBookError):
    code = "INVALID"
    status = 422


class Conflict(AddressBookError):
    code = "CONFLICT"
    status = 409


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _clip(value: object, limit: int) -> str | None:
    """A free-text field from a client: stored as text, bounded, and empty
    means "not set"."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] or None


def normalize_color(value: object) -> int:
    """A tag color as the client stores it: an ARGB integer (`Color.value`).
    Also accepts a numeric string or `#rrggbb`; anything else is the default."""
    text = str(value).strip() if value is not None else ""
    try:
        if text.startswith("#") and len(text) == 7:
            return 0xFF000000 | int(text[1:], 16)
        number = int(text) & 0xFFFFFFFF
    except ValueError:
        return DEFAULT_TAG_COLOR
    return number if number > 0xFFFFFF else 0xFF000000 | number


def _clean_tag_name(value: object) -> str:
    name = _clip(value, MAX_TAG_NAME_LENGTH)
    if not name:
        raise Invalid("A tag needs a name.")
    return name


def _clean_rustdesk_id(value: object) -> str:
    rustdesk_id = value.strip() if isinstance(value, str) else ""
    if not rustdesk_id or len(rustdesk_id) > MAX_ID_LENGTH:
        raise Invalid("A peer needs an id of 1 to 64 characters.")
    return rustdesk_id


# --------------------------------------------------------------------------
# Books
# --------------------------------------------------------------------------


def get_personal_book(db: Session, user_id: int) -> AddressBook:
    """The user's personal book, created on first use."""
    stmt = select(AddressBook).where(AddressBook.owner_id == user_id, AddressBook.is_personal.is_(True))
    book = db.execute(stmt).scalar_one_or_none()
    if book is None:
        try:
            with db.begin_nested():
                book = AddressBook(owner_id=user_id, name=PERSONAL_BOOK_NAME, is_personal=True)
                db.add(book)
        except IntegrityError:
            # A concurrent request (the client and the WebUI both asking on
            # first use) created it first: (owner_id, name) is unique.
            book = db.execute(stmt).scalar_one()
    return book


def rule_for(book: AddressBook, user_id: int, shares: dict[int, int] | None = None) -> int | None:
    """The user's rule on a book, or None when they may not see it at all."""
    if book.owner_id == user_id:
        return RULE_FULL_CONTROL
    if book.is_personal:
        return None
    if shares is not None:
        return shares.get(book.id)
    for share in book.shares:
        if share.user_id == user_id:
            return share.rule
    return None


def find_book(db: Session, guid: str, user_id: int) -> tuple[AddressBook, int] | None:
    book = db.execute(select(AddressBook).where(AddressBook.guid == guid)).scalar_one_or_none()
    if book is None:
        return None
    rule = rule_for(book, user_id)
    return (book, rule) if rule is not None else None


def visible_shared_books(db: Session, user_id: int) -> list[tuple[AddressBook, int]]:
    """Shared (non-personal) books the user owns or that were shared with
    them, with their rule, oldest first."""
    owned = db.execute(
        select(AddressBook).where(AddressBook.owner_id == user_id, AddressBook.is_personal.is_(False))
    ).scalars()
    result = {book.id: (book, RULE_FULL_CONTROL) for book in owned}
    shared = db.execute(
        select(AddressBook, AddressBookShare.rule)
        .join(AddressBookShare, AddressBookShare.address_book_id == AddressBook.id)
        .where(AddressBookShare.user_id == user_id)
    ).all()
    for book, rule in shared:
        result.setdefault(book.id, (book, rule))
    return sorted(result.values(), key=lambda pair: pair[0].id)


def client_profile_names(books: list[tuple[AddressBook, int]], viewer_id: int) -> dict[int, str]:
    """Display names for `/api/ab/shared/profiles`.

    The client keys its books by name (a later profile with the same name
    replaces an earlier one), and reserves the personal book's name. Two
    users may name a book alike, so a name that is not unique for this viewer
    gets its owner's username appended (and the book id as a last resort).
    """
    taken = {PERSONAL_BOOK_NAME.lower()}
    names: dict[int, str] = {}
    # The viewer's own books keep their names; somebody else's book that
    # collides is the one that gets the suffix.
    for book, _rule in sorted(books, key=lambda pair: (pair[0].owner_id != viewer_id, pair[0].id)):
        name = book.name
        if name.lower() in taken:
            name = f"{book.name} ({book.owner.username})"
        if name.lower() in taken:
            name = f"{book.name} ({book.owner.username} #{book.id})"
        taken.add(name.lower())
        names[book.id] = name
    return names


def _validate_book_name(db: Session, owner_id: int, name: object, *, ignore_id: int | None = None) -> str:
    cleaned = _clip(name, 100)
    if not cleaned:
        raise Invalid("An address book needs a name.")
    if cleaned.lower() == PERSONAL_BOOK_NAME.lower():
        raise Conflict(f'"{PERSONAL_BOOK_NAME}" is reserved for your personal address book.')
    stmt = select(AddressBook.id).where(
        AddressBook.owner_id == owner_id, func.lower(AddressBook.name) == cleaned.lower()
    )
    existing = db.execute(stmt).scalar_one_or_none()
    if existing is not None and existing != ignore_id:
        raise Conflict("You already have an address book with this name.")
    return cleaned


def create_shared_book(db: Session, *, owner_id: int, name: object, note: object = None) -> AddressBook:
    book = AddressBook(
        owner_id=owner_id,
        name=_validate_book_name(db, owner_id, name),
        note=_clip(note, 500),
        is_personal=False,
    )
    db.add(book)
    db.flush()
    return book


def update_shared_book(db: Session, book: AddressBook, *, name: object, note: object) -> AddressBook:
    if book.is_personal:
        raise Invalid("The personal address book cannot be renamed.")
    book.name = _validate_book_name(db, book.owner_id, name, ignore_id=book.id)
    book.note = _clip(note, 500)
    db.flush()
    return book


def delete_shared_book(db: Session, book: AddressBook) -> None:
    if book.is_personal:
        raise Invalid("The personal address book cannot be deleted.")
    db.delete(book)
    db.flush()


def set_share(db: Session, book: AddressBook, *, user_id: int, rule: int) -> AddressBookShare:
    if book.is_personal:
        raise Invalid("The personal address book cannot be shared.")
    if rule not in VALID_RULES:
        raise Invalid("The rule must be 1 (read), 2 (read/write) or 3 (full control).")
    if user_id == book.owner_id:
        raise Invalid("The owner already has full access.")
    share = next((s for s in book.shares if s.user_id == user_id), None)
    if share is None:
        share = AddressBookShare(address_book_id=book.id, user_id=user_id, rule=rule)
        db.add(share)
        book.shares.append(share)
    else:
        share.rule = rule
    db.flush()
    return share


def remove_share(db: Session, book: AddressBook, user_id: int) -> bool:
    share = next((s for s in book.shares if s.user_id == user_id), None)
    if share is None:
        return False
    book.shares.remove(share)
    db.delete(share)
    db.flush()
    return True


def entry_counts(db: Session, book_ids: list[int]) -> dict[int, int]:
    if not book_ids:
        return {}
    rows = db.execute(
        select(AddressBookEntry.address_book_id, func.count())
        .where(AddressBookEntry.address_book_id.in_(book_ids))
        .group_by(AddressBookEntry.address_book_id)
    ).all()
    return {book_id: count for book_id, count in rows}


def can_write(rule: int | None) -> bool:
    return rule is not None and rule >= RULE_READ_WRITE


# --------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------


def list_tags(db: Session, book_id: int) -> list[AddressBookTag]:
    stmt = select(AddressBookTag).where(AddressBookTag.address_book_id == book_id).order_by(AddressBookTag.id)
    return list(db.execute(stmt).scalars())


def _ensure_tags(
    db: Session, book: AddressBook, names: list[str], colors: dict[str, int] | None = None
) -> dict[str, AddressBookTag]:
    """The book's tags for these names, creating any that are missing (with
    the given color, else the default)."""
    colors = colors or {}
    existing = {t.name: t for t in list_tags(db, book.id)}
    for name in names:
        if name in existing:
            continue
        tag = AddressBookTag(address_book_id=book.id, name=name, color=colors.get(name, DEFAULT_TAG_COLOR))
        db.add(tag)
        db.flush()
        existing[name] = tag
    return existing


def _clean_tag_names(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    names: list[str] = []
    for value in values:
        name = _clip(value, MAX_TAG_NAME_LENGTH)
        if name and name not in names:
            names.append(name)
    return names


def add_tag(db: Session, book: AddressBook, *, name: object, color: object) -> AddressBookTag:
    """Idempotent: an existing tag just takes the new color."""
    clean = _clean_tag_name(name)
    tags = _ensure_tags(db, book, [clean], {clean: normalize_color(color)})
    tag = tags[clean]
    tag.color = normalize_color(color)
    db.flush()
    return tag


def rename_tag(db: Session, book: AddressBook, *, old: object, new: object) -> AddressBookTag:
    old_name, new_name = _clean_tag_name(old), _clean_tag_name(new)
    tags = {t.name: t for t in list_tags(db, book.id)}
    if old_name not in tags:
        raise NotFound("That tag does not exist.")
    if new_name != old_name and new_name in tags:
        raise Conflict("A tag with that name already exists.")
    tags[old_name].name = new_name
    db.flush()
    return tags[old_name]


def set_tag_color(db: Session, book: AddressBook, *, name: object, color: object) -> AddressBookTag:
    clean = _clean_tag_name(name)
    tag = next((t for t in list_tags(db, book.id) if t.name == clean), None)
    if tag is None:
        raise NotFound("That tag does not exist.")
    tag.color = normalize_color(color)
    db.flush()
    return tag


def delete_tags(db: Session, book: AddressBook, names: object) -> int:
    wanted = set(_clean_tag_names(names))
    deleted = 0
    for tag in list_tags(db, book.id):
        if tag.name in wanted:
            db.delete(tag)
            deleted += 1
    db.flush()
    return deleted


# --------------------------------------------------------------------------
# Entries (peers)
# --------------------------------------------------------------------------


def _entry_query(book_id: int):
    return (
        select(AddressBookEntry)
        .where(AddressBookEntry.address_book_id == book_id)
        .options(selectinload(AddressBookEntry.tags))
        .order_by(AddressBookEntry.id)
    )


def list_entries(db: Session, book_id: int) -> list[AddressBookEntry]:
    return list(db.execute(_entry_query(book_id)).scalars())


def list_entries_page(
    db: Session, book_id: int, *, page: int, page_size: int
) -> tuple[list[AddressBookEntry], int]:
    total = db.execute(
        select(func.count()).select_from(AddressBookEntry).where(AddressBookEntry.address_book_id == book_id)
    ).scalar_one()
    stmt = _entry_query(book_id).offset((page - 1) * page_size).limit(page_size)
    return list(db.execute(stmt).scalars()), total


def get_entry(db: Session, entry_id: int) -> AddressBookEntry | None:
    """Unscoped by design: the caller must check its rule on `entry.book`
    (CLAUDE.md section 65 - IDOR) before doing anything with the result."""
    return db.execute(
        select(AddressBookEntry)
        .where(AddressBookEntry.id == entry_id)
        .options(selectinload(AddressBookEntry.tags))
    ).scalar_one_or_none()


def get_entry_by_rustdesk_id(db: Session, book_id: int, rustdesk_id: str) -> AddressBookEntry | None:
    return db.execute(
        select(AddressBookEntry)
        .where(AddressBookEntry.address_book_id == book_id, AddressBookEntry.rustdesk_id == rustdesk_id)
        .options(selectinload(AddressBookEntry.tags))
    ).scalar_one_or_none()


_TEXT_FIELDS = {"username": 150, "hostname": 255, "platform": 50, "alias": 255, "note": 500}


def _apply_fields(
    db: Session, book: AddressBook, entry: AddressBookEntry, data: dict, box: SecretBox | None = None
) -> None:
    """Sets only the fields present in `data`. `hash` is the personal book's
    client-encrypted blob and is stored as sent. `password` is a shared book's
    connection password, sent in clear by the client: it is stored only
    encrypted, and only when `box` (a configured DATA_ENCRYPTION_KEY) is given -
    without one it is accepted and dropped rather than kept in clear."""
    for field, limit in _TEXT_FIELDS.items():
        if field in data:
            setattr(entry, field, _clip(data[field], limit))
    if "hash" in data and book.is_personal:
        entry.hash = _clip(data["hash"], 255)
    if "password" in data and not book.is_personal and box is not None:
        entry.password_enc = _encrypt_password(box, data["password"])
    if "tags" in data:
        names = _clean_tag_names(data["tags"])
        tags = _ensure_tags(db, book, names)
        entry.tags = [tags[name] for name in names]


def _encrypt_password(box: SecretBox, value: object) -> str | None:
    """An empty or absent value clears the stored password (the client sends
    "" to remove one)."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise Invalid("The password must be a string.")
    if len(value) > MAX_PASSWORD_LENGTH:
        raise Invalid(f"The password is longer than {MAX_PASSWORD_LENGTH} characters.")
    return box.encrypt(value)


def add_peer(db: Session, book: AddressBook, data: dict, box: SecretBox | None = None) -> AddressBookEntry:
    """Idempotent add (CLAUDE.md section 43): an id already in the book is
    updated in place rather than duplicated or rejected."""
    rustdesk_id = _clean_rustdesk_id(data.get("id"))
    entry = get_entry_by_rustdesk_id(db, book.id, rustdesk_id)
    if entry is None:
        entry = AddressBookEntry(address_book_id=book.id, rustdesk_id=rustdesk_id)
        entry.tags = []
        db.add(entry)
    _apply_fields(db, book, entry, data, box)
    db.flush()
    return entry


def update_peer(db: Session, book: AddressBook, data: dict, box: SecretBox | None = None) -> AddressBookEntry:
    """Partial update by id: only the keys the client sent change."""
    rustdesk_id = _clean_rustdesk_id(data.get("id"))
    entry = get_entry_by_rustdesk_id(db, book.id, rustdesk_id)
    if entry is None:
        raise NotFound("That peer is not in this address book.")
    _apply_fields(db, book, entry, data, box)
    db.flush()
    return entry


def delete_peers(db: Session, book: AddressBook, ids: object) -> int:
    if not isinstance(ids, list):
        raise Invalid("Expected a list of peer ids.")
    wanted = {i for i in ids if isinstance(i, str)}
    deleted = 0
    for entry in list_entries(db, book.id):
        if entry.rustdesk_id in wanted:
            db.delete(entry)
            deleted += 1
    db.flush()
    return deleted


def replace_entries(
    db: Session,
    book: AddressBook,
    *,
    entries: list[dict],
    tag_names: list[str],
    tag_colors: dict[str, object],
) -> list[AddressBookEntry]:
    """Legacy `POST /api/ab`: the client pushes its whole list on every save
    (confirmed 2026-09-18 - see docs/rustdesk-compatibility.md), so the book
    is overwritten to match. Existing rows are updated in place (matched by
    rustdesk_id) rather than deleted and recreated, so ids stay stable."""
    wanted_tags = _clean_tag_names(tag_names)
    for entry in entries:
        for name in _clean_tag_names(entry.get("tags")):
            if name not in wanted_tags:
                wanted_tags.append(name)
    colors = {name: normalize_color(color) for name, color in tag_colors.items() if isinstance(name, str)}
    tags = _ensure_tags(db, book, wanted_tags, colors)
    for name, tag in tags.items():
        if name in colors:
            tag.color = colors[name]
    for name in [n for n in tags if n not in wanted_tags]:
        db.delete(tags.pop(name))

    existing = {e.rustdesk_id: e for e in list_entries(db, book.id)}
    seen: set[str] = set()
    for data in entries:
        rid = data.get("id")
        if not isinstance(rid, str) or not rid.strip() or len(rid.strip()) > MAX_ID_LENGTH:
            continue
        rid = rid.strip()
        seen.add(rid)
        record = existing.get(rid)
        if record is None:
            record = AddressBookEntry(address_book_id=book.id, rustdesk_id=rid)
            record.tags = []
            db.add(record)
        # A legacy push always carries these fields, so unset ones clear. It
        # has no `note` (a newer-protocol field), which therefore survives.
        full = {field: data.get(field) for field in ("username", "hostname", "platform", "alias", "hash")}
        _apply_fields(db, book, record, full)
        record.tags = [tags[n] for n in _clean_tag_names(data.get("tags")) if n in tags]

    for rid, record in existing.items():
        if rid not in seen:
            db.delete(record)
    db.flush()
    return list_entries(db, book.id)


def create_entry(
    db: Session,
    book: AddressBook,
    *,
    rustdesk_id: str,
    alias: str | None = None,
    hostname: str | None = None,
    platform: str | None = None,
    note: str | None = None,
    tag_names: list[str] | None = None,
) -> AddressBookEntry:
    """A manually-added entry, created through the WebUI rather than pushed by
    a RustDesk client. The client picks it up the next time it refreshes."""
    if get_entry_by_rustdesk_id(db, book.id, rustdesk_id) is not None:
        raise Conflict("An address book entry with this RustDesk ID already exists.")
    return add_peer(
        db,
        book,
        {
            "id": rustdesk_id,
            "alias": alias,
            "hostname": hostname,
            "platform": platform,
            "note": note,
            "tags": tag_names or [],
        },
    )


def update_entry(
    db: Session,
    book: AddressBook,
    entry: AddressBookEntry,
    *,
    alias: str | None,
    hostname: str | None,
    platform: str | None,
    note: str | None,
    tag_names: list[str],
) -> AddressBookEntry:
    """Full-form-overwrite semantics (like `create_entry`, not a partial
    PATCH) - every field is always supplied by the WebUI's edit form, so
    there is no need to distinguish "omitted" from "cleared"."""
    _apply_fields(
        db,
        book,
        entry,
        {"alias": alias, "hostname": hostname, "platform": platform, "note": note, "tags": tag_names},
    )
    db.flush()
    return entry


def delete_entry(db: Session, entry: AddressBookEntry) -> None:
    db.delete(entry)


# --------------------------------------------------------------------------
# Client wire format
# --------------------------------------------------------------------------


def peer_to_client(entry: AddressBookEntry, book: AddressBook, box: SecretBox | None = None) -> dict:
    """A peer as the client's `Peer.fromJson` reads it. A personal book
    returns its `hash`; a shared book returns the decrypted `password` when
    one is stored and `box` can open it (otherwise it is omitted and the
    client simply asks for the password when connecting)."""
    peer = {
        "id": entry.rustdesk_id,
        "username": entry.username or "",
        "hostname": entry.hostname or "",
        "platform": entry.platform or "",
        "alias": entry.alias or "",
        "tags": [tag.name for tag in entry.tags],
        "note": entry.note or "",
    }
    if book.is_personal:
        peer["hash"] = entry.hash or ""
    elif entry.password_enc and box is not None:
        password = box.decrypt(entry.password_enc)
        if password is None:
            # Key lost or changed. Say which row, never what is in it.
            logger.warning("Cannot decrypt the stored password of address book entry %s.", entry.id)
        else:
            peer["password"] = password
    return peer


def rotate_passwords(db: Session, box: SecretBox) -> tuple[int, int]:
    """Re-encrypts every stored password with the primary key. Returns
    `(rotated, unreadable)`; unreadable rows are left as they are."""
    rotated = unreadable = 0
    for entry in db.execute(
        select(AddressBookEntry).where(AddressBookEntry.password_enc.is_not(None))
    ).scalars():
        token = box.rotate(entry.password_enc) if entry.password_enc else None
        if token is None:
            unreadable += 1
        else:
            entry.password_enc = token
            rotated += 1
    db.flush()
    return rotated, unreadable
