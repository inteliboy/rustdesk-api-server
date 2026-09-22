"""Shared FastAPI dependencies: DB session, current user, CSRF, client IP.

Route handlers should stay thin and compose these dependencies rather than
re-implementing auth/authorization logic inline (CLAUDE.md section 63).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from rustdesk_api.config import Settings, get_settings
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.session import AuthSession
from rustdesk_api.models.user import User
from rustdesk_api.security.permissions import has_permission, requires_2fa_by_role
from rustdesk_api.security.rate_limit import RateLimiter
from rustdesk_api.security.tokens import constant_time_compare
from rustdesk_api.services import tokens as token_service

SESSION_COOKIE_NAME = "rd_session"
CSRF_COOKIE_NAME = "rd_csrf"

# Requests a user whose role requires 2FA, but who hasn't enabled it yet, may still
# make: enough to set it up (or check its status) and to sign out. Everything else
# is blocked with TWO_FACTOR_SETUP_REQUIRED until they enable it (api/two_factor.py,
# prefix /api/v1/auth/2fa, already only requires an interactive session - no admin
# check - so this reuses that flow rather than inventing a new one).
_TWO_FACTOR_SETUP_ALLOWED_PREFIXES = ("/api/v1/auth/2fa",)
_TWO_FACTOR_SETUP_ALLOWED_PATHS = ("/api/v1/auth/logout", "/api/v1/auth/me")


def _two_factor_setup_exempt(path: str) -> bool:
    return path in _TWO_FACTOR_SETUP_ALLOWED_PATHS or path.startswith(_TWO_FACTOR_SETUP_ALLOWED_PREFIXES)


def get_settings_dep() -> Settings:
    return get_settings()


def resolve_client_ip(request: HTTPConnection, settings: Settings) -> str | None:
    """Only trust X-Forwarded-For when the immediate peer is a configured
    trusted proxy (CLAUDE.md section 48)."""
    client_host = request.client.host if request.client else None
    if client_host and client_host in settings.trusted_proxy_list:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return client_host


def get_client_ip(request: Request, settings: Settings = Depends(get_settings_dep)) -> str | None:
    return resolve_client_ip(request, settings)


def resolve_client_scheme(request: HTTPConnection, settings: Settings) -> str:
    """ "http" or "https": how the client reached us. Behind a reverse proxy the
    connection to us is plain HTTP whatever the client used, so the proxy's
    X-Forwarded-Proto is read - but only from a configured trusted proxy, like
    X-Forwarded-For above."""
    client_host = request.client.host if request.client else None
    if client_host and client_host in settings.trusted_proxy_list:
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if proto in ("http", "https"):
            return proto
    return "https" if request.url.scheme in ("https", "wss") else "http"


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def get_optional_session(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    rd_session: str | None = Cookie(default=None),
) -> AuthSession | None:
    bearer = _extract_bearer_token(authorization)
    raw_token = bearer or rd_session
    if not raw_token:
        return None
    session_obj = token_service.get_valid_session(db, raw_token, api_key=True)
    if session_obj is not None and session_obj.kind == token_service.API_KEY_KIND:
        # An API key is for scripts: it is never a cookie (that would make it
        # CSRF-able), and a read-only one cannot change anything.
        if not bearer:
            return None
        if session_obj.scope == "read" and request.method not in ("GET", "HEAD", "OPTIONS"):
            raise ApiError("API_KEY_READ_ONLY", "This API key is read-only.", status.HTTP_403_FORBIDDEN)
    return session_obj


def get_current_user(
    request: Request,
    session_obj: AuthSession | None = Depends(get_optional_session),
) -> User:
    if session_obj is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "NOT_AUTHENTICATED", "message": "Authentication required."}},
        )
    if not session_obj.user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": {"code": "ACCOUNT_DISABLED", "message": "This account is disabled."}},
        )
    user = session_obj.user
    if (
        not user.totp_enabled
        and requires_2fa_by_role(user)
        and not _two_factor_setup_exempt(request.url.path)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "TWO_FACTOR_SETUP_REQUIRED",
                    "message": "Your role requires two-factor authentication. Set it up to continue.",
                }
            },
        )
    return session_obj.user


def get_interactive_user(
    user: User = Depends(get_current_user),
    session_obj: AuthSession | None = Depends(get_optional_session),
) -> User:
    """For endpoints that manage the account itself (two-factor, sessions, API
    keys): a script's API key must not be able to reach them, or a stolen key
    could mint more keys or switch off the second factor."""
    if session_obj is not None and session_obj.kind == token_service.API_KEY_KIND:
        raise ApiError(
            "API_KEY_NOT_ALLOWED",
            "This endpoint cannot be used with an API key; sign in instead.",
            status.HTTP_403_FORBIDDEN,
        )
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "FORBIDDEN",
                    "message": "Administrator privileges are required.",
                }
            },
        )
    return user


def require_permission(area: str, level: str) -> Callable[[User], User]:
    """A dependency for one console area/level of the role permission matrix
    (CLAUDE.md section 66 - the backend, not the WebUI, must enforce this).
    An administrator always passes; a non-admin passes only if a role
    (direct or via a user group) grants at least `level` in `area` -
    see security/permissions.has_permission."""

    def _dependency(user: User = Depends(get_current_user)) -> User:
        if not has_permission(user, area, level):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": {
                        "code": "FORBIDDEN",
                        "message": f"'{level}' access to {area} is required.",
                    }
                },
            )
        return user

    return _dependency


def _get_auth_rate_limiter(request: Request, settings: Settings) -> RateLimiter:
    """Stored on app.state (not a module-level singleton) so it is scoped
    to a single running application instance - a fresh app (e.g. each test)
    gets a fresh limiter instead of silently inheriting exhausted state
    from an unrelated app/test."""
    limiter = getattr(request.app.state, "auth_rate_limiter", None)
    if limiter is None:
        limiter = RateLimiter(
            max_attempts=settings.auth_rate_limit_attempts,
            window_seconds=settings.auth_rate_limit_window_seconds,
        )
        request.app.state.auth_rate_limiter = limiter
    return limiter


def enforce_auth_rate_limit(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> None:
    """Applied to login/registration/password-reset/token endpoints
    (CLAUDE.md section 26). Keyed by client IP; single-process in-memory
    limiter, documented as such rather than pretending it is distributed."""
    limiter = _get_auth_rate_limiter(request, settings)
    key = client_ip or "unknown"
    if not limiter.allow(key):
        raise ApiError(
            "RATE_LIMITED",
            "Too many attempts. Please wait before trying again.",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )


def enforce_client_audit_rate_limit(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> None:
    """For the unauthenticated RustDesk client audit endpoints. A separate
    limiter from the login one so legitimate audit traffic can never lock
    anyone out of logging in (and vice versa)."""
    limiter = getattr(request.app.state, "client_audit_rate_limiter", None)
    if limiter is None:
        limiter = RateLimiter(max_attempts=settings.client_audit_rate_limit_per_minute, window_seconds=60)
        request.app.state.client_audit_rate_limiter = limiter
    if not limiter.allow(client_ip or "unknown"):
        raise ApiError(
            "RATE_LIMITED",
            "Too many requests. Please wait before trying again.",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )


def verify_csrf(
    request: Request,
    authorization: str | None = Header(default=None),
    session_obj: AuthSession | None = Depends(get_optional_session),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> None:
    """CSRF only applies to cookie-authenticated requests. A request that
    authenticates via an explicit Authorization header is not driven by
    ambient browser credentials, so it is not CSRF-exploitable."""
    if _extract_bearer_token(authorization):
        return
    if session_obj is None or session_obj.csrf_token is None:
        return
    if not x_csrf_token or not constant_time_compare(x_csrf_token, session_obj.csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": {"code": "CSRF_FAILED", "message": "CSRF validation failed."}},
        )
