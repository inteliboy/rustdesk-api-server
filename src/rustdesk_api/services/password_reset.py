"""One-time password-reset links.

There is no email sending, so the link is made by an administrator and handed to
the user out of band. Only a hash of the token is stored, the link expires
quickly, works once, and issuing a new one voids the older ones. Redeeming it
ends every session of that user (whoever held a stolen session loses it) and
clears a lockout; two-factor stays on.
"""

from __future__ import annotations

import datetime

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session

from rustdesk_api.models.account import PasswordResetToken
from rustdesk_api.models.user import User
from rustdesk_api.security.tokens import generate_token, hash_token
from rustdesk_api.services import authentication as auth_service
from rustdesk_api.services import tokens as token_service


class ResetError(Exception):
    pass


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def issue(
    db: Session, user: User, *, created_by: User, lifetime_minutes: int
) -> tuple[str, datetime.datetime]:
    """A fresh token for `user` and when it expires. Older links stop working."""
    now = _utcnow()
    db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
    db.execute(delete(PasswordResetToken).where(PasswordResetToken.expires_at < now))
    raw = generate_token()
    expires_at = now + datetime.timedelta(minutes=lifetime_minutes)
    db.add(
        PasswordResetToken(
            user_id=user.id, token_hash=hash_token(raw), created_by_id=created_by.id, expires_at=expires_at
        )
    )
    db.flush()
    return raw, expires_at


def redeem(db: Session, raw_token: str, new_password: str) -> User:
    """Set the new password and spend the token. Raises ResetError (with one
    message for every reason, so a probe learns nothing) if the token is
    unknown, used or expired, or its user is disabled."""
    invalid = ResetError("This reset link is invalid or has expired.")
    if not raw_token:
        raise invalid
    row = db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(raw_token))
    ).scalar_one_or_none()
    if row is None or row.used_at is not None or _as_utc(row.expires_at) <= _utcnow():
        raise invalid
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise invalid
    auth_service.set_password(db, user, new_password)
    auth_service.unlock(user)
    token_service.revoke_all_for_user(db, user.id)
    row.used_at = _utcnow()
    db.execute(
        delete(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id, PasswordResetToken.id != row.id
        )
    )
    db.flush()
    return user


def purge_expired(db: Session) -> int:
    result: CursorResult = db.execute(  # type: ignore[assignment]
        delete(PasswordResetToken).where(PasswordResetToken.expires_at < _utcnow())
    )
    return result.rowcount or 0
