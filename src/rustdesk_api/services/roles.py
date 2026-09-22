"""CRUD for the role permission matrix and user groups.

Kept intentionally admin-only at the API layer (api/roles.py) rather than
delegable through the matrix itself: a user who could create/edit roles could
grant themselves anything, so this stays with is_admin, same as CLAUDE.md
already trusts that flag with everything else.
"""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from rustdesk_api.models.role import PERMISSION_AREAS, PERMISSION_LEVELS, Role
from rustdesk_api.models.user import User
from rustdesk_api.models.user_group import UserGroup


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _clean_permissions(permissions: dict[str, str]) -> dict[str, str]:
    return {
        area: level
        for area, level in permissions.items()
        if area in PERMISSION_AREAS and level in PERMISSION_LEVELS
    }


# --- Roles -------------------------------------------------------------------


def list_roles(db: Session) -> list[Role]:
    return list(db.execute(select(Role).order_by(Role.name)).scalars())


def get_role(db: Session, role_id: int) -> Role | None:
    return db.get(Role, role_id)


def create_role(
    db: Session, *, name: str, description: str | None, permissions: dict[str, str], requires_2fa: bool
) -> Role:
    role = Role(name=name, description=description, requires_2fa=requires_2fa)
    role.permissions = _clean_permissions(permissions)
    db.add(role)
    db.flush()
    return role


def update_role(
    db: Session,
    role: Role,
    *,
    name: str | None = None,
    description: str | None = None,
    permissions: dict[str, str] | None = None,
    requires_2fa: bool | None = None,
) -> Role:
    if name is not None:
        role.name = name
    if description is not None:
        role.description = description
    if permissions is not None:
        role.permissions = _clean_permissions(permissions)
    if requires_2fa is not None:
        role.requires_2fa = requires_2fa
    role.updated_at = _utcnow()
    db.flush()
    return role


def delete_role(db: Session, role: Role) -> None:
    db.delete(role)


# --- User groups ---------------------------------------------------------------


def list_user_groups(db: Session) -> list[UserGroup]:
    return list(db.execute(select(UserGroup).order_by(UserGroup.name)).scalars())


def get_user_group(db: Session, group_id: int) -> UserGroup | None:
    return db.get(UserGroup, group_id)


def create_user_group(db: Session, *, name: str, description: str | None, role_id: int | None) -> UserGroup:
    group = UserGroup(name=name, description=description, role_id=role_id)
    db.add(group)
    db.flush()
    return group


def update_user_group(
    db: Session,
    group: UserGroup,
    *,
    name: str | None = None,
    description: str | None = None,
    role_id: int | None = None,
    clear_role: bool = False,
) -> UserGroup:
    if name is not None:
        group.name = name
    if description is not None:
        group.description = description
    if clear_role:
        group.role_id = None
    elif role_id is not None:
        group.role_id = role_id
    group.updated_at = _utcnow()
    db.flush()
    return group


def delete_user_group(db: Session, group: UserGroup) -> None:
    db.delete(group)


def set_members(db: Session, db_group: UserGroup, user_ids: list[int]) -> None:
    users = list(db.execute(select(User).where(User.id.in_(user_ids))).scalars()) if user_ids else []
    db_group.members = users
    db.flush()
