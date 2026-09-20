"""Putting a device where an administrator wants it: owner, address book, group,
strategy, note.

Two entry points feed this, both from the RustDesk client:

* `rustdesk --assign --token <token> --user_name ... --address_book_name ...`
  posts to `/api/devices/cli` with a bearer token. The caller is authenticated,
  so it is authorised like any other API call: a user can only touch a device
  that is unowned or theirs, only give it to themselves, only use their own
  address books and groups. Anything else needs an administrator.
* Clients built with `preset-*` options put those values in their sysinfo
  upload, which carries no credentials at all. Honouring them is opt-in
  (`ALLOW_SYSINFO_PRESETS`) and limited to a device's first registration, so a
  stranger who knows a device id cannot rearrange an existing one.

Everything is validated before anything is changed, so a request that names a
missing address book does not half-apply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rustdesk_api.models.device import Device
from rustdesk_api.models.group import Group
from rustdesk_api.models.user import User
from rustdesk_api.security.encryption import SecretBox
from rustdesk_api.services import address_book as address_book_service
from rustdesk_api.services import authentication as auth_service
from rustdesk_api.services import strategies as strategy_service

logger = logging.getLogger(__name__)

# The wire keys of a sysinfo upload made by a client with preset options.
PRESET_KEYS = {
    "preset-user-name": "user_name",
    "preset-strategy-name": "strategy_name",
    "preset-address-book-name": "address_book_name",
    "preset-address-book-tag": "address_book_tag",
    "preset-address-book-alias": "address_book_alias",
    "preset-address-book-password": "address_book_password",
    "preset-address-book-note": "address_book_note",
    "preset-device-group-name": "device_group_name",
    "preset-note": "note",
    "preset-device-name": "device_name",
}


class EnrollmentError(Exception):
    """A problem with the request, worded for the person running the command."""


@dataclass
class Assignment:
    user_name: str | None = None
    strategy_name: str | None = None
    address_book_name: str | None = None
    address_book_tag: str | None = None
    address_book_alias: str | None = None
    address_book_password: str | None = None
    address_book_note: str | None = None
    device_group_name: str | None = None
    note: str | None = None
    device_username: str | None = None
    device_name: str | None = None
    applied: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(getattr(self, name) for name in _FIELDS)

    def redacted(self) -> dict:
        """What may be written to an audit entry: names, never the password."""
        return {
            name: getattr(self, name)
            for name in _FIELDS
            if name != "address_book_password" and getattr(self, name)
        }


_FIELDS = (
    "user_name",
    "strategy_name",
    "address_book_name",
    "address_book_tag",
    "address_book_alias",
    "address_book_password",
    "address_book_note",
    "device_group_name",
    "note",
    "device_username",
    "device_name",
)


def assignment_from_body(body: dict, keys: dict[str, str] | None = None) -> Assignment:
    """Reads only text values. `keys` maps wire names to field names (identity
    for `/api/devices/cli`, `PRESET_KEYS` for sysinfo)."""
    assignment = Assignment()
    for wire, name in (keys or {n: n for n in _FIELDS}).items():
        value = body.get(wire)
        if isinstance(value, str) and value.strip():
            setattr(assignment, name, value.strip())
    return assignment


def _find_user(db: Session, name: str) -> User:
    user = auth_service.get_user_by_username(db, name)
    if user is None or not user.is_active:
        raise EnrollmentError(f"User {name!r} was not found.")
    return user


def _find_group(db: Session, owner: User, name: str) -> Group:
    group = db.execute(
        select(Group).where(Group.owner_id == owner.id, func.lower(Group.name) == name.lower())
    ).scalar_one_or_none()
    if group is None:
        raise EnrollmentError(f"{owner.username!r} has no device group named {name!r}.")
    return group


def _find_writable_book(db: Session, owner: User, name: str):
    """A book of `owner`'s that they may add peers to: their personal one (which
    the client calls "My address book") or a shared one with a write rule."""
    wanted = name.lower()
    if wanted == address_book_service.PERSONAL_BOOK_NAME.lower():
        return address_book_service.get_personal_book(db, owner.id)
    for book, rule in address_book_service.visible_shared_books(db, owner.id):
        if book.name.lower() == wanted:
            if not address_book_service.can_write(rule):
                raise EnrollmentError(f"{owner.username!r} may not add to the address book {name!r}.")
            return book
    raise EnrollmentError(f"{owner.username!r} has no address book named {name!r}.")


def apply(
    db: Session,
    device: Device,
    assignment: Assignment,
    *,
    actor: User | None,
    box: SecretBox | None,
    strict: bool = True,
) -> None:
    """Apply `assignment` to `device`.

    `actor` is the authenticated caller, or `None` for the unauthenticated
    preset path (then only an administrator-equivalent scope is assumed, and
    the caller has already limited this to a device's first registration).
    `strict` makes an unusable request an error; without it, parts that cannot
    be honoured are skipped with a log line (a preset must never break a
    device's registration).
    """
    is_admin = actor is None or actor.is_admin

    if actor is not None and not is_admin and device.owner_id not in (None, actor.id):
        raise EnrollmentError("This device belongs to another user.")
    if assignment.device_username:
        raise EnrollmentError("device_username is not supported: the server reports what the client sends.")

    # ---- resolve everything first
    target: User | None = None
    if assignment.user_name:
        target = _find_user(db, assignment.user_name)
        if actor is not None and not is_admin and target.id != actor.id:
            raise EnrollmentError("Only an administrator can assign a device to another user.")
    # Whose address books and groups are meant: the named user, else the
    # device's owner, else the caller.
    scope_user = target or device.owner or actor

    strategy = None
    if assignment.strategy_name:
        if not is_admin:
            raise EnrollmentError("Only an administrator can assign a strategy.")
        strategy = strategy_service.get_by_name(db, assignment.strategy_name)
        if strategy is None:
            raise EnrollmentError(f"There is no strategy named {assignment.strategy_name!r}.")

    group = book = None
    needs_scope = assignment.device_group_name or assignment.address_book_name
    if needs_scope and scope_user is None:
        if strict:
            raise EnrollmentError("Say which user the address book or group belongs to (--user_name).")
        logger.info("Ignoring preset address book/group for device %s: no user to attach it to", device.id)
    elif scope_user is not None:
        try:
            if assignment.device_group_name:
                group = _find_group(db, scope_user, assignment.device_group_name)
            if assignment.address_book_name:
                book = _find_writable_book(db, scope_user, assignment.address_book_name)
        except EnrollmentError as exc:
            if strict:
                raise
            logger.info("Ignoring preset for device %s: %s", device.id, exc)
    if assignment.address_book_password:
        if book is not None and book.is_personal:
            if strict:
                raise EnrollmentError("A saved password needs a shared address book, not the personal one.")
            assignment.address_book_password = None
        elif book is not None and box is None:
            if strict:
                raise EnrollmentError("Saved address book passwords need DATA_ENCRYPTION_KEY on the server.")
            assignment.address_book_password = None

    # ---- apply
    if target is not None and device.owner_id != target.id:
        device.owner_id = target.id
        assignment.applied.append("owner")
    if strategy is not None:
        device.strategy_id = strategy.id
        assignment.applied.append("strategy")
    if group is not None:
        device.group_id = group.id
        assignment.applied.append("group")
    if assignment.note:
        device.note = assignment.note[:500]
        assignment.applied.append("note")
    if assignment.device_name:
        device.alias = assignment.device_name[:255]
        assignment.applied.append("name")
    if book is not None:
        # Only what is known: an update of an existing entry must not blank the
        # fields this request has nothing to say about.
        candidates = {
            "id": device.rustdesk_id,
            "alias": assignment.address_book_alias or assignment.device_name or device.alias,
            "hostname": device.hostname,
            "username": device.username,
            "platform": device.platform,
            "note": assignment.address_book_note,
            "tags": [assignment.address_book_tag] if assignment.address_book_tag else None,
            "password": assignment.address_book_password,
        }
        entry = {key: value for key, value in candidates.items() if value}
        address_book_service.add_peer(db, book, entry, box)
        assignment.applied.append("address_book")
    db.flush()
