"""Admin-only user management (/api/v1/users/...)."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import (
    enforce_auth_rate_limit,
    get_current_user,
    get_settings_dep,
    require_permission,
    verify_csrf,
)
from rustdesk_api.api.schemas import (
    CreateUserRequest,
    ResetPasswordRequest,
    UpdateUserRequest,
    UserOut,
)
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.role import Role
from rustdesk_api.models.user import User
from rustdesk_api.security.permissions import has_permission
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import authentication as auth_service
from rustdesk_api.services import password_reset as reset_service
from rustdesk_api.services import tokens as token_service
from rustdesk_api.services import two_factor as two_factor_service

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db), _user: User = Depends(require_permission("users", "view"))
) -> list[User]:
    return list(db.execute(select(User).order_by(User.username)).scalars())


@router.post("", response_model=UserOut, status_code=201, dependencies=[Depends(verify_csrf)])
def create_user(
    payload: CreateUserRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_permission("users", "manage")),
) -> User:
    if auth_service.get_user_by_username(db, payload.username) is not None:
        raise ApiError("USERNAME_TAKEN", "That username is already in use.", 409)
    if not actor.is_admin and (payload.is_admin or payload.role_id is not None):
        raise ApiError(
            "FORBIDDEN", "Only an administrator can grant administrator status or assign a role.", 403
        )
    if payload.role_id is not None and db.get(Role, payload.role_id) is None:
        raise ApiError("ROLE_NOT_FOUND", "The requested role does not exist.", 404)
    user = auth_service.create_user(
        db,
        username=payload.username,
        password=payload.password,
        email=payload.email,
        is_admin=payload.is_admin,
    )
    if payload.role_id is not None:
        user.role_id = payload.role_id
    audit_service.record(
        db,
        action="user_created",
        actor_id=actor.id,
        target_type="user",
        target_id=user.id,
        detail={"username": user.username},
    )
    db.commit()
    return user


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db), current: User = Depends(get_current_user)) -> User:
    if not current.is_admin and current.id != user_id and not has_permission(current, "users", "view"):
        raise ApiError("USER_NOT_FOUND", "The requested user does not exist.", 404)
    user = db.get(User, user_id)
    if user is None:
        raise ApiError("USER_NOT_FOUND", "The requested user does not exist.", 404)
    return user


@router.patch("/{user_id}", response_model=UserOut, dependencies=[Depends(verify_csrf)])
def update_user(
    user_id: int,
    payload: UpdateUserRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_permission("users", "manage")),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise ApiError("USER_NOT_FOUND", "The requested user does not exist.", 404)

    if payload.is_active is False and user.id == actor.id:
        raise ApiError("CANNOT_DEACTIVATE_SELF", "You cannot deactivate your own account.", 400)
    if payload.is_admin is False and user.id == actor.id:
        raise ApiError("CANNOT_DEMOTE_SELF", "You cannot remove your own administrator role.", 400)
    if not actor.is_admin and (
        payload.is_admin is not None or payload.role_id is not None or payload.clear_role
    ):
        raise ApiError(
            "FORBIDDEN", "Only an administrator can grant administrator status or assign a role.", 403
        )

    if payload.is_active is not None:
        user.is_active = payload.is_active
        if not payload.is_active:
            token_service.revoke_all_for_user(db, user.id)
    if payload.is_admin is not None:
        user.is_admin = payload.is_admin
    if payload.email is not None:
        user.email = payload.email
    if payload.clear_role:
        user.role_id = None
    elif payload.role_id is not None:
        if db.get(Role, payload.role_id) is None:
            raise ApiError("ROLE_NOT_FOUND", "The requested role does not exist.", 404)
        user.role_id = payload.role_id

    audit_service.record(
        db,
        action="user_updated",
        actor_id=actor.id,
        target_type="user",
        target_id=user.id,
        detail={"username": user.username, "is_active": user.is_active, "is_admin": user.is_admin},
    )
    db.commit()
    return user


@router.post(
    "/{user_id}/reset-password",
    response_model=UserOut,
    dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)],
)
def reset_password(
    user_id: int,
    payload: ResetPasswordRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission("users", "manage")),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise ApiError("USER_NOT_FOUND", "The requested user does not exist.", 404)
    auth_service.set_password(db, user, payload.password)
    token_service.revoke_all_for_user(db, user.id)
    audit_service.record(
        db,
        action="user_password_reset",
        actor_id=admin.id,
        target_type="user",
        target_id=user.id,
        detail={"username": user.username},
    )
    db.commit()
    return user


@router.delete("/{user_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def delete_user(
    user_id: int, db: Session = Depends(get_db), admin: User = Depends(require_permission("users", "manage"))
) -> None:
    if user_id == admin.id:
        raise ApiError("CANNOT_DELETE_SELF", "You cannot delete your own account.", 400)
    user = db.get(User, user_id)
    if user is None:
        raise ApiError("USER_NOT_FOUND", "The requested user does not exist.", 404)
    deleted_username = user.username
    db.delete(user)
    audit_service.record(
        db,
        action="user_deleted",
        actor_id=admin.id,
        target_type="user",
        target_id=user_id,
        detail={"username": deleted_username},
    )
    db.commit()


@router.delete("/{user_id}/two-factor", status_code=204, dependencies=[Depends(verify_csrf)])
def reset_two_factor(
    user_id: int, db: Session = Depends(get_db), admin: User = Depends(require_permission("users", "manage"))
) -> None:
    """For a user who lost their authenticator and recovery codes. Turns 2FA off
    and signs them out everywhere, so whoever holds a session made before now
    does not keep it. They can set 2FA up again after signing in."""
    user = db.get(User, user_id)
    if user is None:
        raise ApiError("USER_NOT_FOUND", "The requested user does not exist.", 404)
    if not user.totp_enabled and not user.totp_secret_enc:
        raise ApiError("TWO_FACTOR_NOT_ON", "Two-factor authentication is not on for this user.", 409)
    two_factor_service.disable(db, user)
    token_service.revoke_all_for_user(db, user.id)
    audit_service.record(
        db,
        action="two_factor_reset",
        actor_id=admin.id,
        target_type="user",
        target_id=user.id,
        detail={"username": user.username},
    )
    db.commit()


class ResetLinkOut(BaseModel):
    # Shown once: only a hash of the token is stored. The token sits after the
    # `#`, which a browser never sends to the server, so it stays out of access
    # logs.
    url: str
    expires_at: datetime.datetime


@router.post(
    "/{user_id}/reset-link",
    response_model=ResetLinkOut,
    dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)],
)
def issue_reset_link(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission("users", "manage")),
    settings: Settings = Depends(get_settings_dep),
) -> ResetLinkOut:
    """A one-time link for a user who forgot their password, to hand over out of
    band (nothing is emailed). Setting the password through it signs the user
    out everywhere."""
    user = db.get(User, user_id)
    if user is None:
        raise ApiError("USER_NOT_FOUND", "The requested user does not exist.", 404)
    if not user.is_active:
        raise ApiError("USER_DISABLED", "Activate the account before issuing a reset link.", 409)
    raw, expires_at = reset_service.issue(
        db, user, created_by=admin, lifetime_minutes=settings.password_reset_lifetime_minutes
    )
    audit_service.record(
        db,
        action="password_reset_issued",
        actor_id=admin.id,
        target_type="user",
        target_id=user.id,
        detail={"username": user.username, "minutes": settings.password_reset_lifetime_minutes},
    )
    db.commit()
    return ResetLinkOut(
        url=f"{settings.external_url.rstrip('/')}/reset-password#token={raw}", expires_at=expires_at
    )


@router.post("/{user_id}/unlock", response_model=UserOut, dependencies=[Depends(verify_csrf)])
def unlock_user(
    user_id: int, db: Session = Depends(get_db), admin: User = Depends(require_permission("users", "manage"))
) -> User:
    """Lift a lockout caused by too many wrong passwords."""
    user = db.get(User, user_id)
    if user is None:
        raise ApiError("USER_NOT_FOUND", "The requested user does not exist.", 404)
    auth_service.unlock(user)
    audit_service.record(
        db,
        action="user_unlocked",
        actor_id=admin.id,
        target_type="user",
        target_id=user.id,
        detail={"username": user.username},
    )
    db.commit()
    return user
