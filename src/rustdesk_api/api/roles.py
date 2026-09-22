"""The role permission matrix and user groups (/api/v1/roles, /api/v1/user-groups).

Admin-only (CLAUDE.md section 66 - the WebUI may hide this page from a delegate,
but the backend must not trust that): a role or user-group editor could grant
themselves anything through the matrix, so this stays with is_admin rather than
being delegable through the matrix it manages.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_current_admin, verify_csrf
from rustdesk_api.api.schemas import (
    CreateRoleRequest,
    CreateUserGroupRequest,
    RoleOut,
    UpdateRoleRequest,
    UpdateUserGroupRequest,
    UserGroupOut,
)
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.role import Role
from rustdesk_api.models.user import User
from rustdesk_api.models.user_group import UserGroup
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import roles as role_service

router = APIRouter(prefix="/api/v1", tags=["roles"])


def _group_out(group: UserGroup) -> UserGroupOut:
    out = UserGroupOut.model_validate(group)
    out.member_ids = sorted(member.id for member in group.members)
    return out


@router.get("/roles", response_model=list[RoleOut])
def list_roles(db: Session = Depends(get_db), _admin: User = Depends(get_current_admin)) -> list[Role]:
    return role_service.list_roles(db)


@router.post("/roles", response_model=RoleOut, status_code=201, dependencies=[Depends(verify_csrf)])
def create_role(
    payload: CreateRoleRequest, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)
) -> Role:
    role = role_service.create_role(
        db,
        name=payload.name,
        description=payload.description,
        permissions=payload.permissions,
        requires_2fa=payload.requires_2fa,
    )
    audit_service.record(
        db,
        action="role_created",
        actor_id=admin.id,
        target_type="role",
        target_id=role.id,
        detail={"name": role.name},
    )
    db.commit()
    return role


@router.patch("/roles/{role_id}", response_model=RoleOut, dependencies=[Depends(verify_csrf)])
def update_role(
    role_id: int,
    payload: UpdateRoleRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> Role:
    role = role_service.get_role(db, role_id)
    if role is None:
        raise ApiError("ROLE_NOT_FOUND", "The requested role does not exist.", 404)
    role_service.update_role(
        db,
        role,
        name=payload.name,
        description=payload.description,
        permissions=payload.permissions,
        requires_2fa=payload.requires_2fa,
    )
    audit_service.record(
        db,
        action="role_updated",
        actor_id=admin.id,
        target_type="role",
        target_id=role.id,
        detail={"name": role.name},
    )
    db.commit()
    return role


@router.delete("/roles/{role_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def delete_role(
    role_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)
) -> None:
    role = role_service.get_role(db, role_id)
    if role is None:
        raise ApiError("ROLE_NOT_FOUND", "The requested role does not exist.", 404)
    deleted_name = role.name
    role_service.delete_role(db, role)
    audit_service.record(
        db,
        action="role_deleted",
        actor_id=admin.id,
        target_type="role",
        target_id=role_id,
        detail={"name": deleted_name},
    )
    db.commit()


@router.get("/user-groups", response_model=list[UserGroupOut])
def list_user_groups(
    db: Session = Depends(get_db), _admin: User = Depends(get_current_admin)
) -> list[UserGroupOut]:
    return [_group_out(g) for g in role_service.list_user_groups(db)]


@router.post(
    "/user-groups", response_model=UserGroupOut, status_code=201, dependencies=[Depends(verify_csrf)]
)
def create_user_group(
    payload: CreateUserGroupRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> UserGroupOut:
    if payload.role_id is not None and role_service.get_role(db, payload.role_id) is None:
        raise ApiError("ROLE_NOT_FOUND", "The requested role does not exist.", 404)
    group = role_service.create_user_group(
        db, name=payload.name, description=payload.description, role_id=payload.role_id
    )
    role_service.set_members(db, group, payload.member_ids)
    audit_service.record(
        db,
        action="user_group_created",
        actor_id=admin.id,
        target_type="user_group",
        target_id=group.id,
        detail={"name": group.name},
    )
    db.commit()
    return _group_out(group)


@router.patch("/user-groups/{group_id}", response_model=UserGroupOut, dependencies=[Depends(verify_csrf)])
def update_user_group(
    group_id: int,
    payload: UpdateUserGroupRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> UserGroupOut:
    group = role_service.get_user_group(db, group_id)
    if group is None:
        raise ApiError("USER_GROUP_NOT_FOUND", "The requested user group does not exist.", 404)
    if payload.role_id is not None and role_service.get_role(db, payload.role_id) is None:
        raise ApiError("ROLE_NOT_FOUND", "The requested role does not exist.", 404)
    role_service.update_user_group(
        db,
        group,
        name=payload.name,
        description=payload.description,
        role_id=payload.role_id,
        clear_role=payload.clear_role,
    )
    if payload.member_ids is not None:
        role_service.set_members(db, group, payload.member_ids)
    audit_service.record(
        db,
        action="user_group_updated",
        actor_id=admin.id,
        target_type="user_group",
        target_id=group.id,
        detail={"name": group.name},
    )
    db.commit()
    return _group_out(group)


@router.delete("/user-groups/{group_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def delete_user_group(
    group_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)
) -> None:
    group = role_service.get_user_group(db, group_id)
    if group is None:
        raise ApiError("USER_GROUP_NOT_FOUND", "The requested user group does not exist.", 404)
    deleted_name = group.name
    role_service.delete_user_group(db, group)
    audit_service.record(
        db,
        action="user_group_deleted",
        actor_id=admin.id,
        target_type="user_group",
        target_id=group_id,
        detail={"name": deleted_name},
    )
    db.commit()
