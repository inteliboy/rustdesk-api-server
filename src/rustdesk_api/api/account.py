"""Account self-service under /api/v1/auth: public options, self-registration,
password reset with an administrator-issued link, and the user's own login
sessions."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import (
    enforce_auth_rate_limit,
    get_client_ip,
    get_interactive_user,
    get_optional_session,
    get_settings_dep,
    verify_csrf,
)
from rustdesk_api.api.schemas import SetupRequest
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.session import AuthSession
from rustdesk_api.models.user import User
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import authentication as auth_service
from rustdesk_api.services import password_reset as reset_service
from rustdesk_api.services import tokens as token_service

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# What the sign-in page needs to know
# ---------------------------------------------------------------------------


class AuthOptions(BaseModel):
    registration_enabled: bool
    registration_requires_approval: bool


@router.get("/options", response_model=AuthOptions)
def auth_options(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep)
) -> AuthOptions:
    # Registration is only offered once the first administrator exists: until
    # then /setup is the way in, and nobody may register into an empty server.
    enabled = settings.allow_registration and auth_service.any_users_exist(db)
    return AuthOptions(
        registration_enabled=enabled,
        registration_requires_approval=settings.registration_requires_approval,
    )


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------


class RegisterResponse(BaseModel):
    # "active" (can sign in now) or "pending_approval" (an administrator has to
    # activate the account first).
    status: str


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_auth_rate_limit)],
)
def register(
    payload: SetupRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> RegisterResponse:
    if not settings.allow_registration or not auth_service.any_users_exist(db):
        raise ApiError("REGISTRATION_DISABLED", "Registration is not enabled on this server.", 403)
    if auth_service.get_user_by_username(db, payload.username) is not None:
        raise ApiError("USERNAME_TAKEN", "That username is already in use.", 409)
    email = payload.email.strip() if payload.email else None
    if (
        email
        and db.execute(select(User.id).where(func.lower(User.email) == email.lower())).first() is not None
    ):
        raise ApiError("EMAIL_TAKEN", "That email address is already in use.", 409)

    # Never an administrator, whatever the request says.
    user = auth_service.create_user(
        db, username=payload.username, password=payload.password, email=email or None, is_admin=False
    )
    pending = settings.registration_requires_approval
    user.is_active = not pending
    audit_service.record(
        db,
        action="user_registered",
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        ip_address=client_ip,
        detail={"username": user.username, "pending_approval": pending},
    )
    db.commit()
    return RegisterResponse(status="pending_approval" if pending else "active")


# ---------------------------------------------------------------------------
# Password reset (the link is issued by an administrator: users.py)
# ---------------------------------------------------------------------------


class ResetWithTokenRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=8, max_length=256)


@router.post("/reset-password", status_code=204, dependencies=[Depends(enforce_auth_rate_limit)])
def reset_password_with_token(
    payload: ResetWithTokenRequest,
    db: Session = Depends(get_db),
    client_ip: str | None = Depends(get_client_ip),
) -> None:
    try:
        user = reset_service.redeem(db, payload.token, payload.password)
    except reset_service.ResetError as exc:
        audit_service.record(
            db,
            action="password_reset_completed",
            result="failure",
            ip_address=client_ip,
        )
        db.commit()
        raise ApiError("INVALID_RESET_LINK", str(exc), status.HTTP_400_BAD_REQUEST) from exc
    audit_service.record(
        db,
        action="password_reset_completed",
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        ip_address=client_ip,
        detail={"username": user.username},
    )
    db.commit()


# ---------------------------------------------------------------------------
# The user's own login sessions
# ---------------------------------------------------------------------------

# API keys and enrollment tokens have their own lists.
_LOGIN_KINDS = ("webui", "client")


class SessionOut(BaseModel):
    id: int
    kind: str
    ip_address: str | None
    user_agent: str | None
    created_at: datetime.datetime
    last_used_at: datetime.datetime | None
    expires_at: datetime.datetime
    current: bool


def _own_login_sessions(db: Session, user: User) -> list[AuthSession]:
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = db.execute(
        select(AuthSession)
        .where(
            AuthSession.user_id == user.id,
            AuthSession.kind.in_(_LOGIN_KINDS),
            AuthSession.revoked_at.is_(None),
        )
        .order_by(AuthSession.created_at.desc())
    ).scalars()
    return [s for s in rows if s.is_valid() and _aware(s.expires_at) > now]


def _aware(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(
    db: Session = Depends(get_db),
    user: User = Depends(get_interactive_user),
    current: AuthSession | None = Depends(get_optional_session),
) -> list[SessionOut]:
    return [
        SessionOut(
            id=s.id,
            kind=s.kind,
            ip_address=s.ip_address,
            user_agent=s.user_agent,
            created_at=s.created_at,
            last_used_at=s.last_used_at,
            expires_at=s.expires_at,
            current=current is not None and s.id == current.id,
        )
        for s in _own_login_sessions(db, user)
    ]


@router.delete("/sessions/{session_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def revoke_session(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_interactive_user),
) -> None:
    # Only the caller's own sessions: another user's id answers 404, the same as
    # an id that does not exist.
    target = next((s for s in _own_login_sessions(db, user) if s.id == session_id), None)
    if target is None:
        raise ApiError("SESSION_NOT_FOUND", "The requested session does not exist.", 404)
    token_service.revoke_session(db, target)
    audit_service.record(
        db, action="session_revoked", actor_id=user.id, target_type="session", target_id=target.id
    )
    db.commit()


@router.post("/sessions/revoke-others", dependencies=[Depends(verify_csrf)])
def revoke_other_sessions(
    db: Session = Depends(get_db),
    user: User = Depends(get_interactive_user),
    current: AuthSession | None = Depends(get_optional_session),
) -> dict:
    """Sign out everywhere except here."""
    count = 0
    for s in _own_login_sessions(db, user):
        if current is not None and s.id == current.id:
            continue
        token_service.revoke_session(db, s)
        count += 1
    if count:
        audit_service.record(
            db,
            action="sessions_revoked",
            actor_id=user.id,
            target_type="user",
            target_id=user.id,
            detail={"count": count},
        )
    db.commit()
    return {"revoked": count}
