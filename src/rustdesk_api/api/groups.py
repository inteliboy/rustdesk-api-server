"""Device group management (/api/v1/groups/...).

Groups are owned by a single user (CLAUDE.md section 7). A non-admin only
ever sees/manages their own groups; an admin sees all of them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_current_user, verify_csrf
from rustdesk_api.api.schemas import CreateGroupRequest, GroupOut, UpdateGroupRequest
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.device import Device
from rustdesk_api.models.group import Group
from rustdesk_api.models.user import User
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import groups as group_service

router = APIRouter(prefix="/api/v1/groups", tags=["groups"])


def _device_count(db: Session, group_id: int) -> int:
    return db.execute(select(func.count(Device.id)).where(Device.group_id == group_id)).scalar_one()


def _to_out(db: Session, group: Group) -> GroupOut:
    out = GroupOut.model_validate(group)
    out.device_count = _device_count(db, group.id)
    out.strategy_name = group.strategy.name if group.strategy_id is not None and group.strategy else None
    return out


def _get_owned_or_404(db: Session, group_id: int, user: User) -> Group:
    group = group_service.get_by_id(db, group_id)
    if group is None or (not user.is_admin and group.owner_id != user.id):
        raise ApiError("GROUP_NOT_FOUND", "The requested group does not exist.", 404)
    return group


@router.get("", response_model=list[GroupOut])
def list_groups(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[GroupOut]:
    owner_id = None if user.is_admin else user.id
    groups = group_service.list_groups(db, owner_id=owner_id)
    return [_to_out(db, g) for g in groups]


@router.post("", response_model=GroupOut, status_code=201, dependencies=[Depends(verify_csrf)])
def create_group(
    payload: CreateGroupRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> GroupOut:
    group = group_service.create_group(
        db, owner_id=user.id, name=payload.name, description=payload.description
    )
    audit_service.record(
        db,
        action="group_created",
        actor_id=user.id,
        target_type="group",
        target_id=group.id,
        detail={"name": group.name},
    )
    db.commit()
    return _to_out(db, group)


@router.get("/{group_id}", response_model=GroupOut)
def get_group(
    group_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> GroupOut:
    group = _get_owned_or_404(db, group_id, user)
    return _to_out(db, group)


@router.patch("/{group_id}", response_model=GroupOut, dependencies=[Depends(verify_csrf)])
def update_group(
    group_id: int,
    payload: UpdateGroupRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GroupOut:
    group = _get_owned_or_404(db, group_id, user)
    group_service.update_group(db, group, name=payload.name, description=payload.description)
    audit_service.record(
        db,
        action="group_updated",
        actor_id=user.id,
        target_type="group",
        target_id=group.id,
        detail={"name": group.name},
    )
    db.commit()
    return _to_out(db, group)


@router.delete("/{group_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def delete_group(
    group_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    group = _get_owned_or_404(db, group_id, user)
    deleted_name = group.name
    group_service.delete_group(db, group)
    audit_service.record(
        db,
        action="group_deleted",
        actor_id=user.id,
        target_type="group",
        target_id=group_id,
        detail={"name": deleted_name},
    )
    db.commit()
