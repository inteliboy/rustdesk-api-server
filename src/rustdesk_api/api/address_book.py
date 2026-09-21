"""RustDesk self-hosted "API Server" address-book / peer-management
endpoints.

STATUS: `/api/ab`'s envelope and read/write behavior are now based on the
actual Django view source of the reference project named in CLAUDE.md
section 73 (https://github.com/bryangerlach/rustdesk-api-server,
api/views_api.py::ab) combined with live capture against a real RustDesk
1.4.9 client - see docs/rustdesk-compatibility.md for the full trace.
`login-options`, `device-group/accessible`, `/api/users` and `/api/peers` are
not implemented in the reference project; their shapes come from the client's
own parser (see docs/rustdesk-compatibility.md) and are not yet live-verified
(CLAUDE.md section 74: observe -> test -> document -> implement).

This module holds the *legacy* address book (`GET/POST /api/ab`) and the
smaller client lookups. The newer per-item protocol (personal guid, shared
books, `/api/ab/peers`, ...) is in `address_book_protocol.py`; both work on
the same personal book.

Confirmed (2026-09-18, live capture): personal address book entries are
independent, client-managed records - a user can add an entry for a peer
id this server has never seen report in via /api/sysinfo. They are stored
in the dedicated `AddressBookEntry` model (services/address_book.py), not
derived from `Device`.
"""

from __future__ import annotations

import datetime
import json
import logging

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.orm import Session

from rustdesk_api.db.database import get_db
from rustdesk_api.models.address_book_entry import AddressBook
from rustdesk_api.models.user import User
from rustdesk_api.services import address_book as address_book_service
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import groups as group_service
from rustdesk_api.services import tokens as token_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rustdesk-compat"])


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def current_client_user(db: Session, authorization: str | None) -> User | None:
    raw_token = _extract_bearer(authorization)
    if not raw_token:
        return None
    session_obj = token_service.get_valid_session(db, raw_token)
    return session_obj.user if session_obj is not None else None


def _build_ab_response(db: Session, book: AddressBook) -> dict:
    """Builds the `{"updated_at": ..., "data": "<json string>"}` envelope
    confirmed from the reference project's `ab` view: `data` decodes to
    `{"tags": [...], "peers": [...], "tag_colors": "<json string>"}` -
    `tag_colors` is itself JSON-encoded *again* as a string, matching the
    reference source exactly (`json.dumps(tag_colors)` assigned into a
    dict that is then itself `json.dumps`'d). Colors are the client's ARGB
    integers."""
    tags = address_book_service.list_tags(db, book.id)
    data = {
        "tags": sorted(tag.name for tag in tags),
        "peers": [
            address_book_service.peer_to_client(entry, book)
            for entry in address_book_service.list_entries(db, book.id)
        ],
        "tag_colors": json.dumps({tag.name: tag.color for tag in tags}),
    }
    return {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "data": json.dumps(data),
    }


@router.get("/api/login-options")
def login_options() -> list[str]:
    """The extra ways to sign in: one `oidc/<name>` string per OIDC provider
    (or `common-oidc/<json>`). None are configured (OIDC is not implemented),
    so an empty array.

    The client's only reader, `UserModel.queryOidcLoginOptions`, iterates the
    decoded body (`for (final item in jsonDecode(resp.body))`), so it has to be
    an array: 1.4.9 swallows the error a JSON object causes there, but `master`
    shows a "network error" line and a Retry button in its login dialog."""
    return []


def _accessible_page(items: list[dict], current: int, page_size: int) -> dict:
    """The envelope of the client's "accessible" lookups (`GroupModel` in
    flutter/lib/models/group_model.dart). Unlike `/api/ab`, `data` must be a
    real JSON array: the client does `if (data is List)` and silently skips
    anything else, which is what a JSON-encoded string is, so the panel stayed
    empty."""
    start = (current - 1) * page_size
    return {"total": len(items), "data": items[start : start + page_size]}


@router.get("/api/device-group/accessible")
def device_group_accessible(
    current: int = Query(default=1, ge=1),
    pageSize: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> dict:
    """Device groups shown in the left list of the client's "Accessible
    devices" tab; the client reads only `name` (`DeviceGroupPayload`). The
    caller's own groups (all of them for an administrator)."""
    user = current_client_user(db, authorization)
    if user is None:
        return _accessible_page([], current, pageSize)

    owner_id = None if user.is_admin else user.id
    groups = group_service.list_groups(db, owner_id=owner_id)
    return _accessible_page([{"name": g.name} for g in groups], current, pageSize)


@router.get("/api/ab")
def get_address_book(db: Session = Depends(get_db), authorization: str | None = Header(default=None)) -> dict:
    """Legacy address book (`GET /api/ab`) - the personal book, in the
    envelope confirmed 2026-09-18: a bare empty array/object previously
    caused a real client to raise a Dart type error. Clients that speak the
    newer per-item protocol never call this; older ones (and any client when
    `ADDRESS_BOOK_LEGACY_MODE=true`) use it exclusively."""
    user = current_client_user(db, authorization)
    if user is None:
        return {
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "data": json.dumps({"tags": [], "peers": [], "tag_colors": "{}"}),
        }
    book = address_book_service.get_personal_book(db, user.id)
    response = _build_ab_response(db, book)
    db.commit()  # persists a personal book created by this first read
    return response


@router.post("/api/ab")
async def save_address_book(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> dict:
    """Legacy address book save (confirmed 2026-09-18, live capture): the
    client saves its whole list here, as a POST to the same path
    `GET /api/ab` reads from - matching the reference project's `ab()`
    view, which dispatches on request method. Body: `{"data": "<json string
    of {tags, peers, tag_colors}>"}`, where `tag_colors` is itself a
    JSON-encoded string (double-encoded, same as the read response).

    Only ever touches the personal book, and replaces it wholesale - which is
    why the newer per-item protocol is preferable."""
    user = current_client_user(db, authorization)
    if user is None:
        return {"error": "Not authenticated."}

    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes) if body_bytes else {}
        inner = json.loads(body.get("data", "{}"))
    except (ValueError, TypeError, AttributeError):
        return {"error": "Invalid request body."}
    if not isinstance(inner, dict):
        return {"error": "Invalid request body."}

    peers = inner.get("peers", [])
    tag_names = inner.get("tags", [])
    tag_colors_raw = inner.get("tag_colors", "{}")
    try:
        tag_colors = json.loads(tag_colors_raw) if isinstance(tag_colors_raw, str) else tag_colors_raw
    except ValueError:
        tag_colors = {}
    if not isinstance(peers, list) or not isinstance(tag_colors, dict):
        return {"error": "Invalid request body."}

    book = address_book_service.get_personal_book(db, user.id)
    address_book_service.replace_entries(
        db,
        book,
        entries=[p for p in peers if isinstance(p, dict)],
        tag_names=tag_names if isinstance(tag_names, list) else [],
        tag_colors=tag_colors,
    )
    db.commit()
    # Root-caused via the actual client source (flutter/lib/models/ab_model.dart,
    # LegacyAb.pushAb): success is `resp.statusCode == 200 && (body is empty/null
    # OR the decoded JSON has no "error" key at all)` - if an "error" key is
    # present for ANY reason, even "", the client does `throw json['error']`,
    # which for an empty string renders as "<push_ab_failed_tip>: " with nothing
    # after the colon. That is exactly the banner reported after manually adding
    # an entry - this project's own bug (returning {"error": ""} on success), not
    # a stale/leftover UI banner as previously guessed. Must never include the
    # "error" key on a successful response.
    return {}


def _client_os(platform: str | None) -> str:
    """`PeerPayload.info["os"]`: the client takes the text before " / " and
    matches it, lower-cased, against windows/linux/macos/android to choose the
    icon. Stored platforms are sometimes longer ("Windows 11")."""
    if not platform:
        return ""
    first = platform.split()[0].lower()
    return first if first in ("windows", "linux", "macos", "android") else platform


@router.get("/api/users")
def list_users_for_sharing(
    current: int = Query(default=1, ge=1),
    pageSize: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> dict:
    """Users in the left list of the client's "Accessible devices" tab (fields
    read by `UserPayload`: `name`, `status`, `is_admin`, ...). Choosing one
    shows the peers whose `user_name` equals it. That is the caller plus the
    owners of devices shared with the caller - not every account on the
    server, which would tell any user who else has one."""
    user = current_client_user(db, authorization)
    if user is None:
        return _accessible_page([], current, pageSize)

    others = [u for u in device_service.list_owners_of_shared_devices(db, user.id) if u.id != user.id]
    items = [
        {"name": u.username, "display_name": "", "email": "", "note": "", "status": 1, "is_admin": u.is_admin}
        for u in [user, *others]
    ]
    return _accessible_page(items, current, pageSize)


@router.get("/api/peers")
def list_peers(
    current: int = Query(default=1, ge=1),
    pageSize: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> dict:
    """Devices in the client's "Accessible devices" tab: those the caller owns
    plus those shared with them, one page at a time. Shape as read by
    `PeerPayload` (flutter/lib/common/hbbs/hbbs.dart): the device's own
    facts sit under `info`, and `user_name` / `device_group_name` are what the
    left list filters on. Distinct from the personal address book of `/api/ab`."""
    user = current_client_user(db, authorization)
    if user is None:
        return _accessible_page([], current, pageSize)

    items, total = device_service.list_devices(
        db, owner_id=user.id, shared_with_user_id=user.id, page=current, page_size=pageSize
    )
    data = [
        {
            "id": device.rustdesk_id,
            "info": {
                "username": device.username or "",
                "os": _client_os(device.platform),
                "device_name": device.hostname or device.name or "",
            },
            "status": 1,
            "user": str(device.owner_id) if device.owner_id is not None else "",
            "user_name": device.owner.username if device.owner is not None else "",
            "device_group_name": device.group.name if device.group is not None else "",
            "note": device.note or "",
        }
        for device in items
    ]
    # The server already paged; `total` lets the client ask for the next page.
    return {"total": total, "data": data}
