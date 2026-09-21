"""OpenID Connect sign-in endpoints.

RustDesk client protocol (source: `hbbs_http/account.rs`, `login.dart`; see
docs/rustdesk-compatibility.md):

* `POST /api/oidc/auth` - `{op, id, uuid, deviceInfo, apiDomain}` -> `{code, url}`
  or `{error}`. The client opens `url` in a browser and polls
* `GET /api/oidc/auth-query?code=&id=&uuid=` every second for up to three minutes:
  `{"error": "No authed oidc is found"}` while waiting (that text is what keeps the
  client polling; any other error ends it), then the login body.

The browser leg is ours: `GET /api/oidc/callback`, where the provider sends the
user back. The WebUI has its own start (`/api/v1/auth/oidc/login`, a redirect) and
"link my account" (`POST /api/v1/auth/oidc/link`) using the same callback.

Everything security-relevant (state, PKCE, nonce, token validation, who the user
is) lives in security/oidc.py and services/oidc.py; this module only speaks HTTP.
"""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Cookie, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from rustdesk_api.api.auth import (
    RustdeskLoginRequest,
    _finish_rustdesk_login,
    _start_webui_session,
)
from rustdesk_api.api.deps import (
    enforce_auth_rate_limit,
    get_client_ip,
    get_interactive_user,
    get_settings_dep,
    verify_csrf,
)
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.user import User
from rustdesk_api.security.oidc import OidcError, Provider
from rustdesk_api.security.rate_limit import RateLimiter
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import oidc as oidc_service

logger = logging.getLogger("rustdesk_api.oidc")

router = APIRouter(tags=["oidc"])
v1_router = APIRouter(prefix="/api/v1/auth/oidc", tags=["oidc"])

# Set on the browser that starts a WebUI sign-in or a link, and required back at the
# callback: a sign-in cannot be finished in a different browser than it was started in.
BINDING_COOKIE = "rd_oidc"
BINDING_PATH = "/api/oidc"
# What the client shows while it waits; it keeps polling only for this text.
PENDING_ERROR = "No authed oidc is found"


class OidcAuthRequest(BaseModel):
    op: str | None = None
    id: str | None = None
    uuid: str | None = None
    deviceInfo: dict | None = None
    # Sent by the client and deliberately ignored: the redirect URI is built from
    # EXTERNAL_URL, never from anything a caller supplies.
    apiDomain: str | None = None


def _provider(settings: Settings) -> Provider | None:
    return oidc_service.provider_from_settings(settings)


def _poll_limiter(request: Request, settings: Settings) -> RateLimiter:
    limiter = getattr(request.app.state, "oidc_poll_rate_limiter", None)
    if limiter is None:
        # The client polls once a second; several machines may share an address.
        limiter = RateLimiter(
            max_attempts=max(settings.client_audit_rate_limit_per_minute, 120), window_seconds=60
        )
        request.app.state.oidc_poll_rate_limiter = limiter
    return limiter


def _enforce_poll_rate_limit(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> None:
    if not _poll_limiter(request, settings).allow(client_ip or "unknown"):
        raise ApiError("RATE_LIMITED", "Too many requests. Please wait before trying again.", 429)


def _audit_failure(
    db: Session, *, via: str, reason: str, client_ip: str | None, actor_id: int | None = None
) -> None:
    audit_service.record(
        db,
        action="login",
        actor_id=actor_id,
        result="failure",
        ip_address=client_ip,
        detail={"via": via, "reason": reason},
    )


# ---------------------------------------------------------------------------
# RustDesk client
# ---------------------------------------------------------------------------


@router.post("/api/oidc/auth", dependencies=[Depends(enforce_auth_rate_limit)])
def client_start(
    payload: OidcAuthRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> dict:
    provider = _provider(settings)
    if provider is None:
        return {"error": "Single sign-on is not set up on this server."}
    if not payload.op or payload.op.lower() != provider.name.lower():
        return {"error": "Unknown sign-in provider."}
    if not payload.id or not payload.uuid:
        return {"error": "The device id is missing."}
    try:
        started = oidc_service.begin(
            db,
            provider,
            settings,
            purpose=oidc_service.PURPOSE_CLIENT,
            device_id=payload.id,
            device_uuid=payload.uuid,
        )
    except OidcError as exc:
        logger.warning("oidc start failed: %s", exc.reason)
        _audit_failure(db, via="oidc_client", reason=exc.reason, client_ip=client_ip)
        db.commit()
        return {"error": exc.public}
    db.commit()
    return {"code": started.handle, "url": started.url}


@router.get("/api/oidc/auth-query", dependencies=[Depends(_enforce_poll_rate_limit)])
def client_poll(
    request: Request,
    code: str = Query(default="", max_length=200),
    id: str = Query(default="", max_length=64),  # noqa: A002 - the client's parameter name
    uuid: str = Query(default="", max_length=128),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> dict:
    collected = oidc_service.collect(db, code, id, uuid) if code else None
    if collected is None or collected.state == "unknown":
        return {"error": "The sign-in request was not found or has expired."}
    if collected.state == "pending":
        return {"error": PENDING_ERROR}
    if collected.state == "failed" or collected.user is None or not collected.user.is_active:
        db.commit()  # the failed request is handed over once
        return {"error": collected.message or "The sign-in failed."}

    user = collected.user
    body = _finish_rustdesk_login(
        user,
        RustdeskLoginRequest(id=collected.device_id, uuid=collected.device_uuid),
        request,
        db,
        settings,
        client_ip,
        via="rustdesk_client_oidc",
    )
    # The client's parser needs `info` (an object) and reads the rest when present.
    body["user"] = {
        "name": user.username,
        "id": user.id,
        "email": user.email,
        "status": 1,
        "is_admin": user.is_admin,
        "info": {},
    }
    return body


# ---------------------------------------------------------------------------
# The browser comes back from the provider
# ---------------------------------------------------------------------------


def _page(
    title: str, message: str, *, status_code: int = 200, back: tuple[str, str] | None = None
) -> HTMLResponse:
    link = f'<p><a href="{html.escape(back[0])}">{html.escape(back[1])}</a></p>' if back else ""
    body = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;"
        "color:#0f172a}a{color:#2563eb}</style></head><body>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>{link}</body></html>"
    )
    response = HTMLResponse(body, status_code=status_code)
    # The URL carries the authorization code and state. (The app-wide Referrer-Policy is
    # same-origin, so it is never sent on to another site.)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/api/oidc/callback", dependencies=[Depends(enforce_auth_rate_limit)])
def callback(
    request: Request,
    code: str | None = Query(default=None, max_length=4096),
    state: str | None = Query(default=None, max_length=200),
    error: str | None = Query(default=None, max_length=100),
    binding: str | None = Cookie(default=None, alias=BINDING_COOKIE),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> Response:
    provider = _provider(settings)
    if provider is None:
        return _page("Sign-in unavailable", "Single sign-on is not set up on this server.", status_code=404)

    result = oidc_service.complete(
        db, provider, settings, state=state, code=code, error=error, binding=binding
    )
    via = f"oidc_{result.purpose}" if result.purpose else "oidc"

    if not result.ok or result.user is None:
        logger.info("oidc sign-in refused: %s", result.reason)
        _audit_failure(db, via=via, reason=result.reason, client_ip=client_ip)
        db.commit()
        back = (
            ("/security", "Back to Security") if result.purpose == "link" else ("/login", "Back to sign in")
        )
        page = _page("Sign-in failed", result.message, status_code=400, back=back)
        page.delete_cookie(BINDING_COOKIE, path=BINDING_PATH)
        return page

    user = result.user
    if result.user_created:
        audit_service.record(
            db,
            action="user_created",
            actor_id=user.id,
            target_type="user",
            target_id=user.id,
            ip_address=client_ip,
            detail={"via": "oidc"},
        )
    if result.linked:
        audit_service.record(
            db,
            action="oidc_linked",
            actor_id=user.id,
            target_type="user",
            target_id=user.id,
            ip_address=client_ip,
            detail={"via": via},
        )

    if result.purpose == oidc_service.PURPOSE_WEBUI:
        redirect = RedirectResponse("/", status_code=303)
        _start_webui_session(user, request, redirect, db, settings, client_ip, via="webui_oidc")
        redirect.delete_cookie(BINDING_COOKIE, path=BINDING_PATH)
        redirect.headers["Cache-Control"] = "no-store"
        return redirect
    db.commit()

    if result.purpose == oidc_service.PURPOSE_LINK:
        redirect = RedirectResponse("/security?sso=linked", status_code=303)
        redirect.delete_cookie(BINDING_COOKIE, path=BINDING_PATH)
        return redirect
    return _page("Signed in", "You are signed in. You can close this window and go back to RustDesk.")


# ---------------------------------------------------------------------------
# WebUI
# ---------------------------------------------------------------------------


def _set_binding(response: Response, value: str, settings: Settings) -> None:
    response.set_cookie(
        BINDING_COOKIE,
        value,
        max_age=int(oidc_service.REQUEST_LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",  # sent on the provider's redirect back (a top-level GET)
        secure=settings.secure_cookies,
        path=BINDING_PATH,
    )


@v1_router.get("/login", dependencies=[Depends(enforce_auth_rate_limit)])
def webui_start(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> Response:
    """Sends the browser to the provider. A plain link, so it works without script."""
    provider = _provider(settings)
    if provider is None:
        return RedirectResponse("/login?sso=unavailable", status_code=303)
    try:
        started = oidc_service.begin(db, provider, settings, purpose=oidc_service.PURPOSE_WEBUI)
    except OidcError as exc:
        logger.warning("oidc start failed: %s", exc.reason)
        _audit_failure(db, via="oidc_webui", reason=exc.reason, client_ip=client_ip)
        db.commit()
        return RedirectResponse("/login?sso=unavailable", status_code=303)
    db.commit()
    response = RedirectResponse(started.url, status_code=303)
    _set_binding(response, started.binding or "", settings)
    response.headers["Cache-Control"] = "no-store"
    return response


class LinkStarted(BaseModel):
    url: str


@v1_router.post(
    "/link",
    response_model=LinkStarted,
    dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)],
)
def link_start(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_interactive_user),
    settings: Settings = Depends(get_settings_dep),
) -> LinkStarted:
    provider = _provider(settings)
    if provider is None:
        raise ApiError("OIDC_NOT_CONFIGURED", "Single sign-on is not set up on this server.", 404)
    try:
        started = oidc_service.begin(
            db, provider, settings, purpose=oidc_service.PURPOSE_LINK, user_id=user.id
        )
    except OidcError as exc:
        logger.warning("oidc start failed: %s", exc.reason)
        raise ApiError("OIDC_UNAVAILABLE", exc.public, 502) from exc
    db.commit()
    _set_binding(response, started.binding or "", settings)
    return LinkStarted(url=started.url)


class IdentityOut(BaseModel):
    id: int
    email: str | None
    created_at: str
    last_login_at: str | None


class IdentitiesOut(BaseModel):
    enabled: bool
    name: str | None
    has_password: bool
    identities: list[IdentityOut]


@v1_router.get("/identities", response_model=IdentitiesOut)
def list_identities(
    db: Session = Depends(get_db),
    user: User = Depends(get_interactive_user),
    settings: Settings = Depends(get_settings_dep),
) -> IdentitiesOut:
    provider = _provider(settings)
    return IdentitiesOut(
        enabled=provider is not None,
        name=provider.name if provider else None,
        has_password=oidc_service.has_usable_password(user),
        identities=[
            IdentityOut(
                id=i.id,
                email=i.email,
                created_at=i.created_at.isoformat(),
                last_login_at=i.last_login_at.isoformat() if i.last_login_at else None,
            )
            for i in oidc_service.identities_of(db, user)
        ],
    )


@v1_router.delete("/identities/{identity_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def unlink_identity(
    identity_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_interactive_user),
    client_ip: str | None = Depends(get_client_ip),
) -> None:
    try:
        removed = oidc_service.unlink(db, user, identity_id)
    except oidc_service.LastLoginMethod as exc:
        raise ApiError(
            "LAST_LOGIN_METHOD",
            "This is the only way this account can sign in. Ask an administrator for a reset link first.",
            409,
        ) from exc
    if removed is None:
        raise ApiError("IDENTITY_NOT_FOUND", "No such linked account.", 404)
    audit_service.record(
        db,
        action="oidc_unlinked",
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        ip_address=client_ip,
    )
    db.commit()
