"""Saved device-list views: a name for a set of filters, per user."""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rustdesk_api.models.account import SavedView

MAX_VIEWS_PER_USER = 50


class ViewError(Exception):
    pass


def clean_query(raw: object) -> dict:
    """Only the filters the device list understands, each of the right type.
    Anything else is dropped, so a stored view can never smuggle a parameter
    into the list request the page later builds from it."""
    if not isinstance(raw, dict):
        raise ViewError("The view's filters must be an object.")
    query: dict = {}
    search = raw.get("search")
    if isinstance(search, str) and search.strip():
        query["search"] = search.strip()[:200]
    for key in ("group_id", "tag_id"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            query[key] = value
    if raw.get("status") in ("online", "offline"):
        query["status"] = raw["status"]
    if raw.get("sort") in ("last_seen", "created", "name", "id"):
        query["sort"] = raw["sort"]
    if raw.get("order") in ("asc", "desc"):
        query["order"] = raw["order"]
    return query


def list_views(db: Session, user_id: int) -> list[SavedView]:
    return list(
        db.execute(
            select(SavedView).where(SavedView.user_id == user_id).order_by(func.lower(SavedView.name))
        ).scalars()
    )


def get_own(db: Session, user_id: int, view_id: int) -> SavedView | None:
    return db.execute(
        select(SavedView).where(SavedView.id == view_id, SavedView.user_id == user_id)
    ).scalar_one_or_none()


def create_view(db: Session, user_id: int, name: str, query: object) -> SavedView:
    name = name.strip()
    if not name:
        raise ViewError("A view needs a name.")
    if len(list_views(db, user_id)) >= MAX_VIEWS_PER_USER:
        raise ViewError(f"At most {MAX_VIEWS_PER_USER} saved views per user.")
    cleaned = clean_query(query)
    existing = next((v for v in list_views(db, user_id) if v.name.lower() == name.lower()), None)
    if existing is not None:
        # Saving under a name already used replaces that view.
        existing.query = json.dumps(cleaned)
        db.flush()
        return existing
    view = SavedView(user_id=user_id, name=name[:100], query=json.dumps(cleaned))
    db.add(view)
    db.flush()
    return view


def load_query(view: SavedView) -> dict:
    try:
        return clean_query(json.loads(view.query))
    except (ValueError, ViewError):
        return {}
