"""Authentication endpoints.

Two families live here, sharing the same underlying AuthSession mechanism
(services.tokens) but with different wire formats:

- /api/login, /api/logout, /api/currentUser: the RustDesk client protocol.
  Field names and the "HTTP 200 + error field on failure" convention
  mirror observed behavior of existing community RustDesk API servers
  (CLAUDE.md section 73). NOT YET VERIFIED against a real RustDesk desktop
  client - see docs/rustdesk-compatibility.md.
- /api/v1/auth/*: this project's own WebUI-facing REST API, using normal
  HTTP status codes and cookie-based sessions with CSRF protection.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    enforce_auth_rate_limit,
    get_client_ip,
    get_current_user,
    get_optional_session,
    get_settings_dep,
    verify_csrf,
)
from rustdesk_api.api.schemas import (
    LoginRequest,
    LoginResponse,
    SetupRequest,
    TwoFactorLoginRequest,
    UserOut,
)
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.session import AuthSession
from rustdesk_api.models.user import User
from rustdesk_api.security.encryption import get_secret_box
from rustdesk_api.security.user_agent import audit_client_detail
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import authentication as auth_service
from rustdesk_api.services import client_audit as client_audit_service
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import tokens as token_service
from rustdesk_api.services import two_factor as two_factor_service

logger = logging.getLogger(__name__)

rustdesk_router = APIRouter(tags=["rustdesk-compat"])
v1_router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


# ---------------------------------------------------------------------------
# RustDesk client protocol
# ---------------------------------------------------------------------------


class RustdeskLoginRequest(BaseModel):
    username: str | None = None
    password: str | None = None
    id: str | None = None
    uuid: str | None = None
    autoLogin: bool | None = None
    type: str | None = None
    deviceInfo: dict | None = None
    # The second step of a login to an account with 2FA (`type: "email_code"`):
    # the `secret` from the first response and the code from the authenticator.
    secret: str | None = None
    verificationCode: str | None = None
    tfaCode: str | None = None


class RustdeskIdUuidRequest(BaseModel):
    id: str | None = None
    uuid: str | None = None


def _reported_client_version(db: Session, payload: RustdeskLoginRequest) -> dict[str, str]:
    """The login request carries no client version (only `deviceInfo` os/type/
    name), and the client sends no User-Agent. The version is what the same
    device last reported in `sysinfo`, found by the `id` + `uuid` the login
    does carry - so it is missing for a device that has not reported yet, and
    it is self-reported (display-only, escaped by the WebUI)."""
    if not payload.id:
        return {}
    device = client_audit_service.resolve_reporting_device(db, payload.id, payload.uuid)
    if device is None or not device.client_version:
        return {}
    return {"client_version": device.client_version[:32]}


@rustdesk_router.post("/api/login", dependencies=[Depends(enforce_auth_rate_limit)])
def rustdesk_login(
    payload: RustdeskLoginRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> dict:
    if payload.secret:
        return _rustdesk_second_step(payload, request, db, settings, client_ip)
    if not payload.username or not payload.password:
        return {"error": "Username and password are required."}

    try:
        user = auth_service.authenticate(
            db,
            username=payload.username,
            password=payload.password,
            lockout=auth_service.Lockout.from_settings(settings),
            ip_address=client_ip,
        )
    except (
        auth_service.InvalidCredentials,
        auth_service.AccountDisabled,
        auth_service.AccountLocked,
    ) as exc:
        audit_service.record(
            db,
            action="login",
            result="failure",
            ip_address=client_ip,
            detail={
                "username": payload.username,
                "via": "rustdesk_client",
                **({"reason": "locked"} if isinstance(exc, auth_service.AccountLocked) else {}),
                **_reported_client_version(db, payload),
                **audit_client_detail(request.headers.get("user-agent")),
            },
        )
        db.commit()
        logger.info("rustdesk client login failed for %r", payload.username)
        return {"error": str(exc)}

    if user.totp_enabled:
        return _rustdesk_start_second_step(user, request, db, settings, client_ip)
    return _finish_rustdesk_login(user, payload, request, db, settings, client_ip)


def _rustdesk_start_second_step(
    user: User, request: Request, db: Session, settings: Settings, client_ip: str | None
) -> dict:
    """The password was right but the account has 2FA: answer the way the client
    expects (`type: email_check` + `tfa_type: tfa_check`), which makes it ask for
    the code and repeat the login with the `secret` below."""
    if get_secret_box(settings.data_encryption_key) is None:
        audit_service.record(
            db,
            action="login",
            actor_id=user.id,
            result="failure",
            ip_address=client_ip,
            detail={"via": "rustdesk_client", "reason": "two_factor_unavailable"},
        )
        db.commit()
        return {
            "error": "Two-factor authentication is on for this account but the server cannot check codes "
            "(DATA_ENCRYPTION_KEY is missing). Ask an administrator."
        }
    secret = two_factor_service.create_challenge(db, user)
    db.commit()
    return {
        "type": "email_check",
        "tfa_type": "tfa_check",
        "secret": secret,
        "user": {"name": user.username},
    }


def _rustdesk_second_step(
    payload: RustdeskLoginRequest,
    request: Request,
    db: Session,
    settings: Settings,
    client_ip: str | None,
) -> dict:
    code = payload.tfaCode or payload.verificationCode or ""
    user = two_factor_service.complete_challenge(
        db,
        payload.secret or "",
        code,
        get_secret_box(settings.data_encryption_key),
        username=payload.username,
        allow_recovery=False,  # the client's code field only takes six digits
        lockout=auth_service.Lockout.from_settings(settings),
        ip_address=client_ip,
    )
    if user is None:
        audit_service.record(
            db,
            action="login",
            result="failure",
            ip_address=client_ip,
            detail={"username": payload.username, "via": "rustdesk_client", "two_factor": True},
        )
        db.commit()
        return {"error": "Invalid or expired verification code."}
    return _finish_rustdesk_login(user, payload, request, db, settings, client_ip, two_factor=True)


def _finish_rustdesk_login(
    user: User,
    payload: RustdeskLoginRequest,
    request: Request,
    db: Session,
    settings: Settings,
    client_ip: str | None,
    *,
    two_factor: bool = False,
    via: str = "rustdesk_client",
) -> dict:
    auth_service.register_success(user)
    session_obj, raw_token = token_service.create_session(
        db,
        user=user,
        lifetime_seconds=settings.session_lifetime_seconds,
        kind="client",
        ip_address=client_ip,
        user_agent=request.headers.get("user-agent"),
    )

    # Looked up before the registration below, which may adopt the request's uuid.
    client_version = _reported_client_version(db, payload)

    # If the client reports its own peer id at login time, opportunistically
    # link/register the device and claim ownership for this user (only
    # applies if the device is new or not yet owned - see
    # services.devices.register_or_update).
    if payload.id:
        existing = device_service.get_by_rustdesk_id(db, payload.id)
        vouched = device_service.vouches(user)
        try:
            device_service.register_or_update(
                db,
                rustdesk_id=payload.id,
                uuid=payload.uuid,
                ip_address=client_ip,
                owner_id=user.id,
                uuid_policy=settings.device_uuid_rebind,
                # Signing in proves the account, so it may re-bind a device it owns
                # (or any device, for an administrator) - but not someone else's or
                # an unowned one, which would let any account take over a stranger's id.
                trusted=existing is None or user.is_admin or existing.owner_id == user.id,
                online_timeout=settings.device_online_timeout,
                require_approval=settings.new_device_policy == "approve" and not vouched,
                vouched=vouched,
                pending_limit=settings.new_device_pending_limit,
            )
        except device_service.PendingDevicesFull:
            # The sign-in itself must not fail because the device could not be
            # recorded; it simply is not (see NEW_DEVICE_PENDING_LIMIT).
            logger.warning("Not recording device %s: the approval queue is full", payload.id)

    audit_service.record(
        db,
        action="login",
        actor_id=user.id,
        result="success",
        ip_address=client_ip,
        detail={
            "via": via,
            **({"two_factor": True} if two_factor else {}),
            **client_version,
            **audit_client_detail(request.headers.get("user-agent")),
        },
    )
    db.commit()
    return {
        "access_token": raw_token,
        "type": "access_token",
        "user": {"name": user.username, "id": user.id},
    }


@rustdesk_router.post("/api/logout")
def rustdesk_logout(
    payload: RustdeskIdUuidRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> dict:
    raw_token = _extract_bearer(authorization)
    session_obj = token_service.get_valid_session(db, raw_token) if raw_token else None
    if session_obj is None:
        return {"error": "Not authenticated."}
    token_service.revoke_session(db, session_obj)
    audit_service.record(db, action="logout", actor_id=session_obj.user_id, result="success")
    db.commit()
    return {"code": 1}


@rustdesk_router.post("/api/currentUser")
def rustdesk_current_user(
    payload: RustdeskIdUuidRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> dict:
    raw_token = _extract_bearer(authorization)
    session_obj = token_service.get_valid_session(db, raw_token) if raw_token else None
    if session_obj is None:
        return {"error": "Not authenticated."}
    db.commit()
    return {
        "access_token": raw_token,
        "type": "access_token",
        "name": session_obj.user.username,
    }


# ---------------------------------------------------------------------------
# WebUI / management API
# ---------------------------------------------------------------------------


@v1_router.post(
    "/setup",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_auth_rate_limit)],
)
def setup_first_admin(payload: SetupRequest, db: Session = Depends(get_db)) -> User:
    """Creates the first administrator. Only works while no users exist yet
    (CLAUDE.md section 13). After that, this endpoint always fails."""
    if auth_service.any_users_exist(db):
        raise ApiError("SETUP_ALREADY_COMPLETE", "Setup has already been completed.", 403)
    user = auth_service.create_user(
        db, username=payload.username, password=payload.password, email=payload.email, is_admin=True
    )
    audit_service.record(
        db,
        action="user_created",
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        detail={"via": "setup", "username": user.username},
    )
    db.commit()
    return user


@v1_router.get("/setup/status")
def setup_status(db: Session = Depends(get_db)) -> dict:
    return {"setup_required": not auth_service.any_users_exist(db)}


@v1_router.post("/login", response_model=LoginResponse, dependencies=[Depends(enforce_auth_rate_limit)])
def webui_login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> LoginResponse:
    try:
        user = auth_service.authenticate(
            db,
            username=payload.username,
            password=payload.password,
            lockout=auth_service.Lockout.from_settings(settings),
            ip_address=client_ip,
        )
    except (
        auth_service.InvalidCredentials,
        auth_service.AccountDisabled,
        auth_service.AccountLocked,
    ) as exc:
        audit_service.record(
            db,
            action="login",
            result="failure",
            ip_address=client_ip,
            detail={
                "username": payload.username,
                "via": "webui",
                **({"reason": "locked"} if isinstance(exc, auth_service.AccountLocked) else {}),
                **audit_client_detail(request.headers.get("user-agent")),
            },
        )
        db.commit()
        if isinstance(exc, auth_service.AccountLocked):
            raise ApiError("ACCOUNT_LOCKED", str(exc), status.HTTP_429_TOO_MANY_REQUESTS) from exc
        raise ApiError("INVALID_CREDENTIALS", str(exc), status.HTTP_401_UNAUTHORIZED) from exc

    if user.totp_enabled:
        if get_secret_box(settings.data_encryption_key) is None:
            audit_service.record(
                db,
                action="login",
                actor_id=user.id,
                result="failure",
                ip_address=client_ip,
                detail={"via": "webui", "reason": "two_factor_unavailable"},
            )
            db.commit()
            raise ApiError(
                "TWO_FACTOR_UNAVAILABLE",
                "Two-factor authentication is on for this account but the server cannot check codes "
                "(DATA_ENCRYPTION_KEY is missing). Ask an administrator.",
                status.HTTP_403_FORBIDDEN,
            )
        challenge = two_factor_service.create_challenge(db, user)
        db.commit()
        return LoginResponse(two_factor_required=True, challenge=challenge)
    return _start_webui_session(user, request, response, db, settings, client_ip)


@v1_router.post("/login/2fa", response_model=LoginResponse, dependencies=[Depends(enforce_auth_rate_limit)])
def webui_login_second_factor(
    payload: TwoFactorLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> LoginResponse:
    user = two_factor_service.complete_challenge(
        db,
        payload.challenge,
        payload.code,
        get_secret_box(settings.data_encryption_key),
        lockout=auth_service.Lockout.from_settings(settings),
        ip_address=client_ip,
    )
    if user is None:
        audit_service.record(
            db,
            action="login",
            result="failure",
            ip_address=client_ip,
            detail={
                "via": "webui",
                "two_factor": True,
                **audit_client_detail(request.headers.get("user-agent")),
            },
        )
        db.commit()  # the wrong attempt is counted against the challenge
        raise ApiError(
            "INVALID_CODE",
            "That code is not right, or the sign-in expired. Sign in again.",
            status.HTTP_401_UNAUTHORIZED,
        )
    return _start_webui_session(user, request, response, db, settings, client_ip, two_factor=True)


def _start_webui_session(
    user: User,
    request: Request,
    response: Response,
    db: Session,
    settings: Settings,
    client_ip: str | None,
    *,
    two_factor: bool = False,
    via: str = "webui",
) -> LoginResponse:
    auth_service.register_success(user)
    session_obj, raw_token = token_service.create_session(
        db,
        user=user,
        lifetime_seconds=settings.session_lifetime_seconds,
        kind="webui",
        ip_address=client_ip,
        user_agent=request.headers.get("user-agent"),
        with_csrf=True,
    )
    audit_service.record(
        db,
        action="login",
        actor_id=user.id,
        result="success",
        ip_address=client_ip,
        detail={
            "via": via,
            **({"two_factor": True} if two_factor else {}),
            **audit_client_detail(request.headers.get("user-agent")),
        },
    )
    db.commit()

    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        max_age=settings.session_lifetime_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        session_obj.csrf_token or "",
        max_age=settings.session_lifetime_seconds,
        httponly=False,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )
    return LoginResponse(
        access_token=raw_token, csrf_token=session_obj.csrf_token, user=UserOut.model_validate(user)
    )


@v1_router.post("/logout", dependencies=[Depends(verify_csrf)])
def webui_logout(
    response: Response,
    db: Session = Depends(get_db),
    session_obj: AuthSession | None = Depends(get_optional_session),
) -> dict:
    if session_obj is not None:
        token_service.revoke_session(db, session_obj)
        audit_service.record(db, action="logout", actor_id=session_obj.user_id, result="success")
        db.commit()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
    return {"ok": True}


@v1_router.get("/me", response_model=UserOut)
def whoami(user: User = Depends(get_current_user)) -> User:
    return user
