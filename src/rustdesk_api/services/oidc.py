"""OpenID Connect sign-in: the requests in progress, the identities linked to users,
and deciding which local user a provider's answer is.

Three kinds of request share one table (see models/oidc.py):

* `client`: the RustDesk client asked (`POST /api/oidc/auth`) and polls for the
  result (`GET /api/oidc/auth-query`) with a handle that only it has seen.
* `webui`: a browser asked to sign in to the WebUI.
* `link`: a signed-in user attaches their provider account to their own account.

Who a provider account is (`resolve_user`), in this order and nothing else:

1. The linked identity for (issuer, subject). The subject never changes for an
   account; an e-mail address can.
2. Only when `OIDC_LINK_BY_EMAIL` is on: a local user with the same *verified* e-mail
   address (and an allowed domain). Off by default: whoever controls an address at
   the provider could otherwise take over the local account that uses it.
3. Only when `OIDC_AUTO_CREATE_USERS` is on: a new, non-admin user, for a verified
   address in an allowed domain (the settings refuse to start without the domain list).

Otherwise the sign-in is refused. An existing local account is never linked, and an
administrator never created, by a claim alone.
"""

from __future__ import annotations

import datetime
import hmac
import logging
import re
import secrets
from dataclasses import dataclass

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.orm import Session

from rustdesk_api.config import Settings
from rustdesk_api.models.oidc import OidcIdentity, OidcRequest
from rustdesk_api.models.user import User
from rustdesk_api.security import oidc
from rustdesk_api.security.oidc import OidcError, Provider
from rustdesk_api.security.tokens import generate_token, hash_token
from rustdesk_api.services import authentication as auth_service

logger = logging.getLogger("rustdesk_api.oidc")

REQUEST_LIFETIME = datetime.timedelta(minutes=10)
# Accounts made by a provider sign-in cannot sign in with a password until one is
# set (an administrator's reset link): this is not a valid hash, so no password matches.
UNUSABLE_PASSWORD = "!sso-only"

PURPOSE_CLIENT = "client"
PURPOSE_WEBUI = "webui"
PURPOSE_LINK = "link"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def provider_from_settings(settings: Settings) -> Provider | None:
    """The configured provider, or None when OIDC is off."""
    if not settings.oidc_issuer or not settings.oidc_client_id:
        return None
    return Provider(
        name=settings.oidc_name,
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        scopes=settings.oidc_scopes,
        redirect_uri=settings.external_url.rstrip("/") + "/api/oidc/callback",
        auto_create_users=settings.oidc_auto_create_users,
        link_by_email=settings.oidc_link_by_email,
        allowed_email_domains=frozenset(settings.oidc_allowed_email_domain_list),
        username_claim=settings.oidc_username_claim,
        timeout_seconds=settings.oidc_timeout_seconds,
    )


def has_usable_password(user: User) -> bool:
    return user.has_password


# ---------------------------------------------------------------------------
# Starting a sign-in
# ---------------------------------------------------------------------------


@dataclass
class Started:
    url: str
    # The client's poll handle (client requests) or the browser cookie value (others).
    handle: str | None = None
    binding: str | None = None


def begin(
    db: Session,
    provider: Provider,
    settings: Settings,
    *,
    purpose: str,
    device_id: str | None = None,
    device_uuid: str | None = None,
    user_id: int | None = None,
) -> Started:
    discovery = oidc.discover(provider)  # OidcError if the provider cannot be used
    state = generate_token()
    salt = secrets.token_urlsafe(24)
    handle = generate_token() if purpose == PURPOSE_CLIENT else None
    binding = generate_token() if purpose != PURPOSE_CLIENT else None
    now = _utcnow()
    db.add(
        OidcRequest(
            state_hash=hash_token(state),
            handle_hash=hash_token(handle) if handle else None,
            salt=salt,
            purpose=purpose,
            binding_hash=hash_token(binding) if binding else None,
            device_id=(device_id or None) and device_id[:64],
            device_uuid=(device_uuid or None) and device_uuid[:128],
            user_id=user_id,
            created_at=now,
            expires_at=now + REQUEST_LIFETIME,
        )
    )
    db.flush()
    url = oidc.authorization_url(provider, discovery, state=state, salt=salt, secret_key=settings.secret_key)
    return Started(url=url, handle=handle, binding=binding)


# ---------------------------------------------------------------------------
# The provider's answer
# ---------------------------------------------------------------------------


@dataclass
class CallbackResult:
    ok: bool
    purpose: str | None = None
    user: User | None = None
    # Safe to show the person; `reason` is for the audit log.
    message: str = ""
    reason: str = ""
    user_created: bool = False
    linked: bool = False


_IDP_ERROR = re.compile(r"^[a-z_]{1,40}$")


def _fail(row: OidcRequest | None, public: str, reason: str) -> CallbackResult:
    if row is not None:
        row.status = "failed"
        row.error = public[:200]
    return CallbackResult(ok=False, purpose=row.purpose if row else None, message=public, reason=reason)


def complete(
    db: Session,
    provider: Provider,
    settings: Settings,
    *,
    state: str | None,
    code: str | None,
    error: str | None,
    binding: str | None,
) -> CallbackResult:
    """Handles the browser coming back from the provider. Never raises for a bad
    sign-in: the result says what happened. The request is claimed (committed as
    `exchanging`) before the provider is contacted, so a replayed `state` cannot
    run the exchange twice."""
    row = None
    if state:
        row = db.execute(
            select(OidcRequest).where(OidcRequest.state_hash == hash_token(state))
        ).scalar_one_or_none()
    if row is None or row.status != "pending" or _as_utc(row.expires_at) < _utcnow():
        # Unknown, replayed or expired; nothing to update.
        return CallbackResult(
            ok=False,
            message="This sign-in link is no longer valid. Start again.",
            reason="state_invalid",
        )

    if row.purpose != PURPOSE_CLIENT and not (
        binding and row.binding_hash and hmac.compare_digest(hash_token(binding), row.binding_hash)
    ):
        # Another browser than the one that started it: a login-CSRF attempt or a
        # copied link. The request is left alone, so the real browser can still finish.
        return CallbackResult(
            ok=False,
            purpose=row.purpose,
            message="This sign-in was started in a different browser. Start again.",
            reason="binding_mismatch",
        )

    row.status = "exchanging"
    db.commit()

    if error:
        reason = f"provider_{error}" if _IDP_ERROR.match(error) else "provider_error"
        return _fail(row, "The identity provider did not allow the sign-in.", reason)
    if not code:
        return _fail(row, "The identity provider did not allow the sign-in.", "no_code")

    try:
        discovery = oidc.discover(provider)
        id_token = oidc.exchange_code(
            provider, discovery, code=code, salt=row.salt, secret_key=settings.secret_key
        )
        claims = oidc.validate_id_token(
            provider, discovery, id_token, salt=row.salt, secret_key=settings.secret_key
        )
        user, created, linked = resolve_user(db, provider, row, claims)
    except OidcError as exc:
        return _fail(row, exc.public, exc.reason)
    except Exception:  # noqa: BLE001 - see below
        # Anything unexpected from the provider's answer (a key type we cannot read, a
        # malformed document) must end this sign-in, not leave it "exchanging" while
        # the RustDesk client polls until it times out. Logged with the traceback.
        logger.exception("oidc sign-in failed unexpectedly")
        return _fail(row, "The sign-in failed. Try again.", "internal_error")

    if not user.is_active:
        return _fail(row, "This account is disabled.", "account_disabled")
    row.status = "done"
    if row.purpose != PURPOSE_LINK:
        row.user_id = user.id
    return CallbackResult(
        ok=True, purpose=row.purpose, user=user, user_created=created, linked=linked, reason="ok"
    )


def _email_of(claims: dict) -> str | None:
    value = claims.get("email")
    if not isinstance(value, str) or "@" not in value or len(value) > 255:
        return None
    return value.strip()


def _domain_allowed(provider: Provider, email: str) -> bool:
    if not provider.allowed_email_domains:
        return True
    return email.rsplit("@", 1)[-1].lower() in provider.allowed_email_domains


def resolve_user(db: Session, provider: Provider, row: OidcRequest, claims: dict) -> tuple[User, bool, bool]:
    """(user, created, linked) for a validated ID token, or OidcError."""
    subject = claims["sub"]
    email = _email_of(claims) if oidc.email_verified(claims) else None
    identity = db.execute(
        select(OidcIdentity).where(OidcIdentity.issuer == provider.issuer, OidcIdentity.subject == subject)
    ).scalar_one_or_none()

    if row.purpose == PURPOSE_LINK:
        target = db.get(User, row.user_id) if row.user_id else None
        if target is None or not target.is_active:
            raise OidcError("This account cannot be linked.", "link_user_gone")
        if identity is not None:
            if identity.user_id != target.id:
                raise OidcError("That account is already linked to another user.", "identity_taken")
            _touch(identity, email)
            return target, False, False
        if _identity_of(db, target.id, provider.issuer) is not None:
            raise OidcError("Another account is already linked. Unlink it first.", "user_has_identity")
        _add_identity(db, target, provider, subject, email)
        return target, False, True

    if identity is not None:
        user = db.get(User, identity.user_id)
        if user is None:
            raise OidcError("No account is linked to this sign-in.", "identity_orphaned")
        _touch(identity, email)
        return user, False, False

    if email is None or not _domain_allowed(provider, email):
        raise OidcError(
            "No account is linked to this sign-in. Ask an administrator, or link it from Security.",
            "not_linked",
        )
    if provider.link_by_email:
        existing = db.execute(
            select(User).where(func.lower(User.email) == email.lower())
        ).scalar_one_or_none()
        if existing is not None:
            if _identity_of(db, existing.id, provider.issuer) is not None:
                raise OidcError("No account is linked to this sign-in.", "email_user_has_identity")
            _add_identity(db, existing, provider, subject, email)
            return existing, False, True
    if provider.auto_create_users:
        taken = db.execute(select(User.id).where(func.lower(User.email) == email.lower())).first()
        if taken is not None:
            # The address belongs to a local account that is not linked: say nothing more.
            raise OidcError("No account is linked to this sign-in.", "email_in_use")
        user = User(
            username=_free_username(db, provider, claims, email, subject),
            email=email,
            password_hash=UNUSABLE_PASSWORD,
            is_admin=False,
            is_active=True,
        )
        db.add(user)
        db.flush()
        _add_identity(db, user, provider, subject, email)
        return user, True, True
    raise OidcError(
        "No account is linked to this sign-in. Ask an administrator, or link it from Security.",
        "not_linked",
    )


def _identity_of(db: Session, user_id: int, issuer: str) -> OidcIdentity | None:
    return db.execute(
        select(OidcIdentity).where(OidcIdentity.user_id == user_id, OidcIdentity.issuer == issuer)
    ).scalar_one_or_none()


def _add_identity(db: Session, user: User, provider: Provider, subject: str, email: str | None) -> None:
    now = _utcnow()
    db.add(
        OidcIdentity(
            user_id=user.id,
            issuer=provider.issuer,
            subject=subject,
            email=email,
            created_at=now,
            last_login_at=now,
        )
    )
    db.flush()


def _touch(identity: OidcIdentity, email: str | None) -> None:
    identity.last_login_at = _utcnow()
    if email:
        identity.email = email


def _free_username(db: Session, provider: Provider, claims: dict, email: str, subject: str) -> str:
    candidates = [claims.get(provider.username_claim), email.split("@", 1)[0], f"sso-{subject[:8]}"]
    base = ""
    for candidate in candidates:
        if isinstance(candidate, str):
            cleaned = re.sub(r"[^A-Za-z0-9._@-]", "", candidate)[:100]
            if len(cleaned) >= 3:
                base = cleaned
                break
    base = base or f"sso-{secrets.token_hex(4)}"
    name, n = base, 1
    while auth_service.get_user_by_username(db, name) is not None:
        n += 1
        name = f"{base}-{n}"
    return name


# ---------------------------------------------------------------------------
# What the RustDesk client collects
# ---------------------------------------------------------------------------


@dataclass
class Collected:
    # "pending" (keep polling), "unknown" (no such request: stop), "failed" or "done".
    state: str
    user: User | None = None
    message: str = ""
    device_id: str | None = None
    device_uuid: str | None = None


def collect(db: Session, handle: str, device_id: str | None, device_uuid: str | None) -> Collected:
    """The poll. A handle answers once with a result; a wrong id/uuid answers like an
    unknown handle, so a leaked handle is useless from another device."""
    row = db.execute(
        select(OidcRequest).where(
            OidcRequest.handle_hash == hash_token(handle), OidcRequest.purpose == PURPOSE_CLIENT
        )
    ).scalar_one_or_none()
    if row is None or _as_utc(row.expires_at) < _utcnow():
        return Collected("unknown", message="The sign-in request was not found or has expired.")
    if (row.device_id or "") != (device_id or "") or (row.device_uuid or "") != (device_uuid or ""):
        return Collected("unknown", message="The sign-in request was not found or has expired.")
    if row.status in ("pending", "exchanging"):
        return Collected("pending")
    if row.status == "failed":
        message = row.error or "The sign-in failed."
        db.delete(row)
        return Collected("failed", message=message)
    user = db.get(User, row.user_id) if row.user_id else None
    result = Collected(
        "done" if user is not None else "failed",
        user=user,
        message="" if user is not None else "The sign-in failed.",
        device_id=row.device_id,
        device_uuid=row.device_uuid,
    )
    db.delete(row)  # a result is handed over once
    return result


# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------


class LastLoginMethod(Exception):
    """Unlinking would leave an account with no way to sign in."""


def identities_of(db: Session, user: User) -> list[OidcIdentity]:
    return list(
        db.execute(
            select(OidcIdentity).where(OidcIdentity.user_id == user.id).order_by(OidcIdentity.created_at)
        ).scalars()
    )


def unlink(db: Session, user: User, identity_id: int) -> OidcIdentity | None:
    """Removes one of the user's own identities (another user's id finds nothing)."""
    identity = db.execute(
        select(OidcIdentity).where(OidcIdentity.id == identity_id, OidcIdentity.user_id == user.id)
    ).scalar_one_or_none()
    if identity is None:
        return None
    if not has_usable_password(user) and len(identities_of(db, user)) <= 1:
        raise LastLoginMethod
    db.delete(identity)
    return identity


def purge_expired(db: Session, now: datetime.datetime | None = None) -> int:
    """Deletes requests that expired more than a day ago (a finished but uncollected
    result is useless after its ten minutes; the extra day is only for diagnosing)."""
    cutoff = (now or _utcnow()) - datetime.timedelta(days=1)
    result: CursorResult = db.execute(delete(OidcRequest).where(OidcRequest.expires_at < cutoff))  # type: ignore[assignment]
    return result.rowcount
