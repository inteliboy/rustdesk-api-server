"""Management view of address books for the WebUI.

`/api/v1/address-book` - the entries of one book (the caller's personal book
by default, or `?book=<guid>` for a shared one they can see). Lets the WebUI
show what actually landed in the database from a client's sync, independent
of the client's own UI (useful for verifying sync worked, since the client's
own "failed to sync" banner can be stale - see docs/rustdesk-compatibility.md),
and add/edit/delete entries directly. Confirmed live (2026-09-18) that a
manually-added entry reaches a real client the next time it opens/refreshes
its Address Book tab.

`/api/v1/address-books` - the books themselves: the caller's personal book,
shared books they own, and books shared with them, plus creating, renaming,
deleting and sharing (owner only) shared books.

Every operation is checked against the caller's rule on the entry's book, and
a book the caller has no rule on is reported as not found (CLAUDE.md
section 65 - IDOR).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_current_user, verify_csrf
from rustdesk_api.api.schemas import (
    AddressBookEntryOut,
    AddressBookOut,
    AddressBookShareOut,
    AddressBookTagOut,
    CreateAddressBookEntryRequest,
    CreateAddressBookRequest,
    SetAddressBookShareRequest,
    UpdateAddressBookEntryRequest,
    UpdateAddressBookRequest,
)
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.address_book_entry import AddressBook, AddressBookEntry
from rustdesk_api.models.user import User
from rustdesk_api.security.permissions import can_view_device
from rustdesk_api.services import address_book as address_book_service
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import authentication as auth_service
from rustdesk_api.services import devices as device_service

router = APIRouter(prefix="/api/v1/address-book", tags=["address-book"])
books_router = APIRouter(prefix="/api/v1/address-books", tags=["address-book"])


def _book_not_found() -> ApiError:
    return ApiError("BOOK_NOT_FOUND", "The requested address book does not exist.", 404)


def _read_only() -> ApiError:
    return ApiError("BOOK_READ_ONLY", "You only have read access to this address book.", 403)


def _resolve_book(db: Session, user: User, guid: str | None, *, write: bool = False) -> AddressBook:
    """The book a request is about: the caller's personal book when no guid
    is given, else that book if the caller has a rule on it."""
    if guid is None:
        return address_book_service.get_personal_book(db, user.id)
    found = address_book_service.find_book(db, guid, user.id)
    if found is None:
        raise _book_not_found()
    book, rule = found
    if write and not address_book_service.can_write(rule):
        raise _read_only()
    return book


def _owned_shared_book(db: Session, user: User, guid: str) -> AddressBook:
    """A shared book the caller owns - the only kind whose settings and shares
    they may change. A book they merely have access to is "not found" here
    too, so its existence is not confirmed."""
    found = address_book_service.find_book(db, guid, user.id)
    if found is None or found[0].owner_id != user.id:
        raise _book_not_found()
    return found[0]


def _entry_out(entry: AddressBookEntry) -> AddressBookEntryOut:
    return AddressBookEntryOut(
        id=entry.id,
        rustdesk_id=entry.rustdesk_id,
        username=entry.username,
        hostname=entry.hostname,
        platform=entry.platform,
        alias=entry.alias,
        note=entry.note,
        tags=[AddressBookTagOut(name=t.name, color=t.color_hex) for t in entry.tags],
        has_password=entry.has_password,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _book_out(book: AddressBook, user: User, rule: int, count: int) -> AddressBookOut:
    is_owner = book.owner_id == user.id
    return AddressBookOut(
        guid=book.guid,
        name=book.name,
        note=book.note,
        owner=book.owner.username,
        is_personal=book.is_personal,
        rule=rule,
        can_write=address_book_service.can_write(rule),
        is_owner=is_owner,
        entry_count=count,
        shares=[
            AddressBookShareOut(user_id=s.user_id, username=s.user.username, rule=s.rule)
            for s in sorted(book.shares, key=lambda s: s.user.username.lower())
        ]
        if is_owner
        else [],
    )


def _audit(db: Session, user: User, action: str, book: AddressBook, **detail: object) -> None:
    audit_service.record(
        db,
        action=action,
        actor_id=user.id,
        target_type="address_book",
        target_id=book.id,
        detail={"book": book.name, **detail},
    )


# --------------------------------------------------------------------------
# Books
# --------------------------------------------------------------------------


@books_router.get("", response_model=list[AddressBookOut])
def list_books(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[AddressBookOut]:
    personal = address_book_service.get_personal_book(db, user.id)
    db.commit()  # persists a personal book created by this first look
    others = address_book_service.visible_shared_books(db, user.id)
    pairs = [(personal, 3), *others]
    counts = address_book_service.entry_counts(db, [b.id for b, _ in pairs])
    return [_book_out(book, user, rule, counts.get(book.id, 0)) for book, rule in pairs]


@books_router.post("", response_model=AddressBookOut, status_code=201, dependencies=[Depends(verify_csrf)])
def create_book(
    payload: CreateAddressBookRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> AddressBookOut:
    book = address_book_service.create_shared_book(db, owner_id=user.id, name=payload.name, note=payload.note)
    _audit(db, user, "address_book_created", book)
    db.commit()
    return _book_out(book, user, 3, 0)


@books_router.patch("/{guid}", response_model=AddressBookOut, dependencies=[Depends(verify_csrf)])
def update_book(
    guid: str,
    payload: UpdateAddressBookRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AddressBookOut:
    book = _owned_shared_book(db, user, guid)
    old_name = book.name
    address_book_service.update_shared_book(db, book, name=payload.name, note=payload.note)
    _audit(db, user, "address_book_updated", book, previous_name=old_name)
    db.commit()
    counts = address_book_service.entry_counts(db, [book.id])
    return _book_out(book, user, 3, counts.get(book.id, 0))


@books_router.delete("/{guid}", status_code=204, dependencies=[Depends(verify_csrf)])
def delete_book(guid: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> None:
    book = _owned_shared_book(db, user, guid)
    _audit(db, user, "address_book_deleted", book, shared_with=len(book.shares))
    address_book_service.delete_shared_book(db, book)
    db.commit()


@books_router.put("/{guid}/shares", response_model=AddressBookOut, dependencies=[Depends(verify_csrf)])
def set_book_share(
    guid: str,
    payload: SetAddressBookShareRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AddressBookOut:
    """Give a user access at a rule (or change the rule they already have)."""
    book = _owned_shared_book(db, user, guid)
    target = auth_service.get_user_by_username(db, payload.username)
    if target is None or not target.is_active:
        raise ApiError("USER_NOT_FOUND", "No user with that username exists.", 404)
    address_book_service.set_share(db, book, user_id=target.id, rule=payload.rule)
    _audit(db, user, "address_book_shared", book, username=target.username, rule=payload.rule)
    db.commit()
    counts = address_book_service.entry_counts(db, [book.id])
    return _book_out(book, user, 3, counts.get(book.id, 0))


@books_router.delete(
    "/{guid}/shares/{user_id}", response_model=AddressBookOut, dependencies=[Depends(verify_csrf)]
)
def remove_book_share(
    guid: str, user_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> AddressBookOut:
    book = _owned_shared_book(db, user, guid)
    share = next((s for s in book.shares if s.user_id == user_id), None)
    if share is None:
        raise ApiError("SHARE_NOT_FOUND", "That book is not shared with that user.", 404)
    username = share.user.username
    address_book_service.remove_share(db, book, user_id)
    _audit(db, user, "address_book_unshared", book, username=username)
    db.commit()
    counts = address_book_service.entry_counts(db, [book.id])
    return _book_out(book, user, 3, counts.get(book.id, 0))


# --------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------


@router.get("", response_model=list[AddressBookEntryOut])
def list_entries(
    book: str | None = Query(default=None, description="Address book guid; the personal book when omitted."),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AddressBookEntryOut]:
    target = _resolve_book(db, user, book)
    db.commit()  # persists a personal book created by this first look
    entries = address_book_service.list_entries(db, target.id)
    devices = device_service.get_by_rustdesk_ids(db, [e.rustdesk_id for e in entries])
    out = []
    for entry in entries:
        item = _entry_out(entry)
        device = devices.get(entry.rustdesk_id)
        # Same visibility rule as the devices API: an entry never reveals a
        # device the caller could not open (CLAUDE.md section 65).
        if device is not None and can_view_device(user, device):
            item.device_id = device.id
        out.append(item)
    return out


@router.post("", response_model=AddressBookEntryOut, status_code=201, dependencies=[Depends(verify_csrf)])
def create_entry(
    payload: CreateAddressBookEntryRequest,
    book: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AddressBookEntryOut:
    target = _resolve_book(db, user, book, write=True)
    if address_book_service.get_entry_by_rustdesk_id(db, target.id, payload.rustdesk_id) is not None:
        raise ApiError("ENTRY_EXISTS", "An address book entry with this RustDesk ID already exists.", 409)
    entry = address_book_service.create_entry(
        db,
        target,
        rustdesk_id=payload.rustdesk_id,
        alias=payload.alias,
        hostname=payload.hostname,
        platform=payload.platform,
        note=payload.note,
        tag_names=payload.tags,
    )
    audit_service.record(
        db,
        action="address_book_entry_created",
        actor_id=user.id,
        target_type="address_book_entry",
        target_id=entry.id,
        detail={"rustdesk_id": entry.rustdesk_id, "alias": entry.alias, "book": target.name},
    )
    db.commit()
    return _entry_out(entry)


def _writable_entry(db: Session, user: User, entry_id: int) -> AddressBookEntry:
    """The entry, if the caller may change it. An entry in a book the caller
    cannot see is "not found"; one they can only read is refused."""
    entry = address_book_service.get_entry(db, entry_id)
    rule = address_book_service.rule_for(entry.book, user.id) if entry is not None else None
    if entry is None or rule is None:
        raise ApiError("ENTRY_NOT_FOUND", "The requested address book entry does not exist.", 404)
    if not address_book_service.can_write(rule):
        raise _read_only()
    return entry


@router.patch("/{entry_id}", response_model=AddressBookEntryOut, dependencies=[Depends(verify_csrf)])
def update_entry(
    entry_id: int,
    payload: UpdateAddressBookEntryRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AddressBookEntryOut:
    entry = _writable_entry(db, user, entry_id)
    address_book_service.update_entry(
        db,
        entry.book,
        entry,
        alias=payload.alias,
        hostname=payload.hostname,
        platform=payload.platform,
        note=payload.note,
        tag_names=payload.tags,
    )
    audit_service.record(
        db,
        action="address_book_entry_updated",
        actor_id=user.id,
        target_type="address_book_entry",
        target_id=entry.id,
        detail={"rustdesk_id": entry.rustdesk_id, "alias": entry.alias, "book": entry.book.name},
    )
    db.commit()
    return _entry_out(entry)


@router.delete("/{entry_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def delete_entry(
    entry_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    entry = _writable_entry(db, user, entry_id)
    detail = {"rustdesk_id": entry.rustdesk_id, "alias": entry.alias, "book": entry.book.name}
    address_book_service.delete_entry(db, entry)
    audit_service.record(
        db,
        action="address_book_entry_deleted",
        actor_id=user.id,
        target_type="address_book_entry",
        target_id=entry_id,
        detail=detail,
    )
    db.commit()
