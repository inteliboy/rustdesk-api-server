"""Tag management (/api/v1/tags/...).

Tags are a global, shared vocabulary (no owner - CLAUDE.md section 7 lists
no owner_id for Tags, unlike Group). Any authenticated user may create a
tag and attach it to devices they can edit; renaming or deleting a tag is
admin-only, since that affects every user's tagging, not just the
requester's own devices.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_current_admin, get_current_user, verify_csrf
from rustdesk_api.api.schemas import CreateTagRequest, TagOut, UpdateTagRequest
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.user import User
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import tags as tag_service

router = APIRouter(prefix="/api/v1/tags", tags=["tags"])


@router.get("", response_model=list[TagOut])
def list_tags(db: Session = Depends(get_db), _user: User = Depends(get_current_user)) -> list[TagOut]:
    return [TagOut.model_validate(t) for t in tag_service.list_tags(db)]


@router.post("", response_model=TagOut, status_code=201, dependencies=[Depends(verify_csrf)])
def create_tag(
    payload: CreateTagRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> TagOut:
    if tag_service.get_by_name(db, payload.name) is not None:
        raise ApiError("TAG_NAME_TAKEN", "A tag with that name already exists.", 409)
    tag = tag_service.create_tag(db, name=payload.name, color=payload.color)
    audit_service.record(
        db,
        action="tag_created",
        actor_id=user.id,
        target_type="tag",
        target_id=tag.id,
        detail={"name": tag.name},
    )
    db.commit()
    return TagOut.model_validate(tag)


@router.patch("/{tag_id}", response_model=TagOut, dependencies=[Depends(verify_csrf)])
def update_tag(
    tag_id: int,
    payload: UpdateTagRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> TagOut:
    tag = tag_service.get_by_id(db, tag_id)
    if tag is None:
        raise ApiError("TAG_NOT_FOUND", "The requested tag does not exist.", 404)
    tag_service.update_tag(db, tag, name=payload.name, color=payload.color)
    audit_service.record(
        db,
        action="tag_updated",
        actor_id=admin.id,
        target_type="tag",
        target_id=tag.id,
        detail={"name": tag.name},
    )
    db.commit()
    return TagOut.model_validate(tag)


@router.delete("/{tag_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def delete_tag(tag_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)) -> None:
    tag = tag_service.get_by_id(db, tag_id)
    if tag is None:
        raise ApiError("TAG_NOT_FOUND", "The requested tag does not exist.", 404)
    deleted_name = tag.name
    tag_service.delete_tag(db, tag)
    audit_service.record(
        db,
        action="tag_deleted",
        actor_id=admin.id,
        target_type="tag",
        target_id=tag_id,
        detail={"name": deleted_name},
    )
    db.commit()
