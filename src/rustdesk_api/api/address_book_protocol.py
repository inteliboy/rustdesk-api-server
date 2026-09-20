"""RustDesk client address-book protocol, newer per-item version.

Source-verified 2026-09-20 against the client (`flutter/lib/models/ab_model.dart`,
`flutter/lib/common/hbbs/hbbs.dart`, `flutter/lib/models/peer_model.dart`,
`flutter/lib/utils/http_service.dart`) - see docs/rustdesk-compatibility.md.
Not yet exercised by a live client.

How the client picks a mode: it POSTs `/api/ab/personal`. A JSON body with a
`guid` means this server speaks the newer protocol below; a 404 (or no
`guid`) drops it to the *legacy* address book (`GET/POST /api/ab`, in
`address_book.py`). `ADDRESS_BOOK_LEGACY_MODE=true` makes this server answer
404 here (and on everything below) so clients stay in legacy mode.

Wire details that matter:

- Reads are `POST` with an empty body: `/api/ab/personal` -> `{guid}`,
  `/api/ab/settings` -> `{max_peer_one_ab}`, `/api/ab/shared/profiles` and
  `/api/ab/peers` -> `{total, data: [...]}` paginated with `current`
  (1-based) and `pageSize`, `/api/ab/tags/{guid}` -> a bare list.
- Writes must answer **200 with an empty body** on success. The client
  decodes anything else as JSON and reads `["error"]`; even `{}` would be
  shown as the error "null".
- Errors are `{"error": "<text>"}` with a non-200 status; a 401 makes the
  client sign out, so it is used only for a missing/invalid token.
- Tag colors are ARGB integers. `hash` is stored for the personal book only.
  A shared book's `password` (sent and read back in clear by the client) is
  stored encrypted when DATA_ENCRYPTION_KEY is set, and dropped otherwise.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from rustdesk_api.api.address_book import current_client_user
from rustdesk_api.api.deps import get_settings_dep
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.models.address_book_entry import AddressBook
from rustdesk_api.models.user import User
from rustdesk_api.security.encryption import SecretBox, get_secret_box
from rustdesk_api.services import address_book as ab

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rustdesk-compat"])


class _Unauthenticated(Exception):
    pass


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _protocol(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turns expected failures into the client's `{"error": "..."}` shape."""

    def handle(exc: Exception) -> JSONResponse:
        if isinstance(exc, _Unauthenticated):
            return _error(401, "Not authenticated.")
        assert isinstance(exc, ab.AddressBookError)
        return _error(exc.status, exc.message)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except (_Unauthenticated, ab.AddressBookError) as exc:
                return handle(exc)

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (_Unauthenticated, ab.AddressBookError) as exc:
            return handle(exc)

    return wrapper


def _require(db: Session, settings: Settings, authorization: str | None) -> User:
    if settings.address_book_legacy_mode:
        raise ab.NotFound("Not supported.")
    user = current_client_user(db, authorization)
    if user is None:
        raise _Unauthenticated
    return user


def _box(settings: Settings) -> SecretBox | None:
    return get_secret_box(settings.data_encryption_key)


def _book(db: Session, user: User, guid: str, *, write: bool = False) -> AddressBook:
    """Same answer for a book that does not exist and one the caller may not
    see, so a guid is never confirmed to someone without access."""
    found = ab.find_book(db, guid, user.id)
    if found is None:
        raise ab.NotFound("Address book not found.")
    book, rule = found
    if write and not ab.can_write(rule):
        raise ab.Forbidden("You only have read access to this address book.")
    return book


async def _json_body(request: Request) -> Any:
    raw = await request.body()
    try:
        return json.loads(raw) if raw else None
    except ValueError as exc:
        raise ab.Invalid("The request body is not valid JSON.") from exc


async def _json_object(request: Request) -> dict:
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise ab.Invalid("Expected a JSON object.")
    return body


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@router.post("/api/ab/personal")
@_protocol
def personal_book(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    authorization: str | None = Header(default=None),
) -> Any:
    user = _require(db, settings, authorization)
    book = ab.get_personal_book(db, user.id)
    db.commit()
    return {"guid": book.guid}


@router.post("/api/ab/settings")
@_protocol
def book_settings(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    authorization: str | None = Header(default=None),
) -> Any:
    _require(db, settings, authorization)
    # 0 = no limit per book.
    return {"max_peer_one_ab": 0}


@router.post("/api/ab/shared/profiles")
@_protocol
def shared_profiles(
    current: int = Query(default=1, ge=1),
    pageSize: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    authorization: str | None = Header(default=None),
) -> Any:
    user = _require(db, settings, authorization)
    books = ab.visible_shared_books(db, user.id)
    names = ab.client_profile_names(books, user.id)
    page = books[(current - 1) * pageSize : current * pageSize]
    return {
        "total": len(books),
        "data": [
            {
                "guid": book.guid,
                "name": names[book.id],
                "owner": book.owner.username,
                "note": book.note or "",
                "rule": rule,
                "info": None,
            }
            for book, rule in page
        ],
    }


# --------------------------------------------------------------------------
# Reading one book
# --------------------------------------------------------------------------


@router.post("/api/ab/peers")
@_protocol
def book_peers(
    current: int = Query(default=1, ge=1),
    pageSize: int = Query(default=100, ge=1, le=500),
    ab_guid: str = Query(default="", alias="ab"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    authorization: str | None = Header(default=None),
) -> Any:
    user = _require(db, settings, authorization)
    book = _book(db, user, ab_guid)
    entries, total = ab.list_entries_page(db, book.id, page=current, page_size=pageSize)
    return {"total": total, "data": [ab.peer_to_client(entry, book, _box(settings)) for entry in entries]}


@router.post("/api/ab/tags/{guid}")
@_protocol
def book_tags(
    guid: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    authorization: str | None = Header(default=None),
) -> Any:
    user = _require(db, settings, authorization)
    book = _book(db, user, guid)
    return [{"name": tag.name, "color": tag.color} for tag in ab.list_tags(db, book.id)]


# --------------------------------------------------------------------------
# Peer changes (need read/write or full control)
# --------------------------------------------------------------------------


@router.post("/api/ab/peer/add/{guid}")
@_protocol
async def add_peer(
    guid: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    authorization: str | None = Header(default=None),
) -> Any:
    user = _require(db, settings, authorization)
    book = _book(db, user, guid, write=True)
    ab.add_peer(db, book, await _json_object(request), _box(settings))
    db.commit()
    return Response(status_code=200)


@router.put("/api/ab/peer/update/{guid}")
@_protocol
async def update_peer(
    guid: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    authorization: str | None = Header(default=None),
) -> Any:
    user = _require(db, settings, authorization)
    book = _book(db, user, guid, write=True)
    ab.update_peer(db, book, await _json_object(request), _box(settings))
    db.commit()
    return Response(status_code=200)


@router.delete("/api/ab/peer/{guid}")
@_protocol
async def delete_peers(
    guid: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    authorization: str | None = Header(default=None),
) -> Any:
    user = _require(db, settings, authorization)
    book = _book(db, user, guid, write=True)
    ab.delete_peers(db, book, await _json_body(request))
    db.commit()
    return Response(status_code=200)


# --------------------------------------------------------------------------
# Tag changes
# --------------------------------------------------------------------------


@router.post("/api/ab/tag/add/{guid}")
@_protocol
async def add_tag(
    guid: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    authorization: str | None = Header(default=None),
) -> Any:
    user = _require(db, settings, authorization)
    book = _book(db, user, guid, write=True)
    body = await _json_object(request)
    ab.add_tag(db, book, name=body.get("name"), color=body.get("color"))
    db.commit()
    return Response(status_code=200)


@router.put("/api/ab/tag/rename/{guid}")
@_protocol
async def rename_tag(
    guid: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    authorization: str | None = Header(default=None),
) -> Any:
    user = _require(db, settings, authorization)
    book = _book(db, user, guid, write=True)
    body = await _json_object(request)
    ab.rename_tag(db, book, old=body.get("old"), new=body.get("new"))
    db.commit()
    return Response(status_code=200)


@router.put("/api/ab/tag/update/{guid}")
@_protocol
async def update_tag(
    guid: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    authorization: str | None = Header(default=None),
) -> Any:
    user = _require(db, settings, authorization)
    book = _book(db, user, guid, write=True)
    body = await _json_object(request)
    ab.set_tag_color(db, book, name=body.get("name"), color=body.get("color"))
    db.commit()
    return Response(status_code=200)


@router.delete("/api/ab/tag/{guid}")
@_protocol
async def delete_tags(
    guid: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    authorization: str | None = Header(default=None),
) -> Any:
    user = _require(db, settings, authorization)
    book = _book(db, user, guid, write=True)
    body = await _json_body(request)
    if not isinstance(body, list):
        raise ab.Invalid("Expected a list of tag names.")
    ab.delete_tags(db, book, body)
    db.commit()
    return Response(status_code=200)
