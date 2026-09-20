"""Saved device-list views (/api/v1/views). Strictly per user: a view id that
belongs to someone else is a 404."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_current_user, verify_csrf
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.account import SavedView
from rustdesk_api.models.user import User
from rustdesk_api.services import saved_views as view_service

router = APIRouter(prefix="/api/v1/views", tags=["views"])


class ViewOut(BaseModel):
    id: int
    name: str
    query: dict


class SaveViewRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    query: dict = Field(default_factory=dict)


def _to_out(view: SavedView) -> ViewOut:
    return ViewOut(id=view.id, name=view.name, query=view_service.load_query(view))


@router.get("", response_model=list[ViewOut])
def list_views(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[ViewOut]:
    return [_to_out(v) for v in view_service.list_views(db, user.id)]


@router.post("", response_model=ViewOut, status_code=201, dependencies=[Depends(verify_csrf)])
def save_view(
    payload: SaveViewRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ViewOut:
    try:
        view = view_service.create_view(db, user.id, payload.name, payload.query)
    except view_service.ViewError as exc:
        raise ApiError("INVALID_VIEW", str(exc), 422) from exc
    db.commit()
    return _to_out(view)


@router.delete("/{view_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def delete_view(view_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> None:
    view = view_service.get_own(db, user.id, view_id)
    if view is None:
        raise ApiError("VIEW_NOT_FOUND", "The requested view does not exist.", 404)
    db.delete(view)
    db.commit()
