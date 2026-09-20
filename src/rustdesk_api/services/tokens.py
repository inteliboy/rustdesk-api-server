from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from rustdesk_api.models.session import AuthSession
from rustdesk_api.models.user import User
from rustdesk_api.security.tokens import generate_csrf_token, generate_token, hash_token


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def create_session(
    db: Session,
    *,
    user: User,
    lifetime_seconds: int,
    kind: str = "webui",
    ip_address: str | None = None,
    user_agent: str | None = None,
    with_csrf: bool = False,
    label: str | None = None,
    scope: str = "full",
) -> tuple[AuthSession, str]:
    """Create a new auth session and return (record, raw_token).

    The raw token is only available here, at creation time - it is never
    persisted or logged.
    """
    raw_token = generate_token()
    record = AuthSession(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        csrf_token=generate_csrf_token() if with_csrf else None,
        kind=kind,
        label=label[:100] if label else None,
        scope=scope,
        ip_address=ip_address,
        user_agent=user_agent[:255] if user_agent else None,
        expires_at=_utcnow() + datetime.timedelta(seconds=lifetime_seconds),
    )
    db.add(record)
    db.flush()
    return record, raw_token


ENROLLMENT_KIND = "enroll"
API_KEY_KIND = "apikey"


def get_valid_session(
    db: Session, raw_token: str, *, enrollment: bool = False, api_key: bool = False
) -> AuthSession | None:
    """The live session for a raw token. Enrollment tokens (`kind="enroll"`) are
    only honoured when the caller asks for them - which only the device
    assignment endpoint does - so a token handed to a deployment script cannot
    be used to browse the API as its owner. Likewise API keys (`kind="apikey"`)
    are honoured only by the management API (`/api/v1`), never by the RustDesk
    client endpoints."""
    if not raw_token:
        return None
    token_hash = hash_token(raw_token)
    stmt = select(AuthSession).where(AuthSession.token_hash == token_hash)
    session_obj = db.execute(stmt).scalar_one_or_none()
    if session_obj is None or not session_obj.is_valid():
        return None
    if (session_obj.kind == ENROLLMENT_KIND) != enrollment:
        return None
    if session_obj.kind == API_KEY_KIND and not api_key:
        return None
    session_obj.last_used_at = _utcnow()
    return session_obj


def revoke_session(db: Session, session_obj: AuthSession) -> None:
    session_obj.revoked_at = _utcnow()


def revoke_all_for_user(db: Session, user_id: int) -> int:
    stmt = select(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    count = 0
    for session_obj in db.execute(stmt).scalars():
        session_obj.revoked_at = _utcnow()
        count += 1
    return count


def cleanup_expired(db: Session) -> int:
    """Delete sessions that expired or were revoked more than a day ago.
    Safe to call periodically as a lightweight maintenance task."""
    cutoff = _utcnow() - datetime.timedelta(days=1)
    stmt = select(AuthSession).where(
        (AuthSession.expires_at < cutoff)
        | ((AuthSession.revoked_at.is_not(None)) & (AuthSession.revoked_at < cutoff))
    )
    count = 0
    for session_obj in db.execute(stmt).scalars():
        db.delete(session_obj)
        count += 1
    return count
