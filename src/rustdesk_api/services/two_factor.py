"""Two-factor authentication (TOTP) for logins.

The flow, for both the WebUI and the RustDesk client:

1. The password step succeeds for a user with 2FA on. Instead of a session, a
   short-lived, single-use *challenge* is created and its secret handed back.
2. The second request presents that secret with a code from the authenticator
   app (or, in the WebUI, a recovery code). Only then does a session exist.

The TOTP secret is stored encrypted with `DATA_ENCRYPTION_KEY` (it has to be
readable to verify codes, so hashing is not an option), which is why 2FA cannot
be switched on without that key. If the key is later lost, logins for accounts
that have 2FA fail closed rather than skipping the second factor; an
administrator recovers them with `rustdesk-api disable-2fa`.
"""

from __future__ import annotations

import datetime
import secrets

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from rustdesk_api.models.two_factor import LoginChallenge, RecoveryCode
from rustdesk_api.models.user import User
from rustdesk_api.security import totp
from rustdesk_api.security.encryption import SecretBox
from rustdesk_api.security.tokens import generate_token, hash_token
from rustdesk_api.services import authentication as auth_service

CHALLENGE_LIFETIME = datetime.timedelta(minutes=5)
# Wrong codes a challenge survives. Six digits over five tries is one in
# 200,000; past that the password step has to be repeated.
MAX_FAILED_ATTEMPTS = 5
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes: i l o 0 1


class TwoFactorError(Exception):
    pass


class NotAvailable(TwoFactorError):
    """No DATA_ENCRYPTION_KEY, so a secret can be neither stored nor read."""


class AlreadyEnabled(TwoFactorError):
    pass


class NotEnabled(TwoFactorError):
    pass


class BadCode(TwoFactorError):
    pass


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def require_box(box: SecretBox | None) -> SecretBox:
    if box is None:
        raise NotAvailable("Two-factor authentication needs DATA_ENCRYPTION_KEY to be set on the server.")
    return box


# --------------------------------------------------------------------------
# Enrolment
# --------------------------------------------------------------------------


def begin_setup(db: Session, user: User, box: SecretBox | None) -> str:
    """A fresh secret for the user to add to an authenticator app. It only takes
    effect once `enable` proves the app produces matching codes; calling this
    again before then simply replaces it."""
    box = require_box(box)
    if user.totp_enabled:
        raise AlreadyEnabled("Two-factor authentication is already on.")
    secret = totp.generate_secret()
    user.totp_secret_enc = box.encrypt(secret)
    user.totp_last_step = None
    db.flush()
    return secret


def pending_secret(user: User, box: SecretBox | None) -> str | None:
    if box is None or not user.totp_secret_enc:
        return None
    return box.decrypt(user.totp_secret_enc)


def enable(db: Session, user: User, box: SecretBox | None, code: str) -> list[str]:
    """Turn 2FA on once `code` matches the pending secret; returns the recovery
    codes, which are not shown again."""
    box = require_box(box)
    if user.totp_enabled:
        raise AlreadyEnabled("Two-factor authentication is already on.")
    secret = pending_secret(user, box)
    if secret is None:
        raise NotEnabled("Start the setup first.")
    step = totp.verify(secret, code)
    if step is None:
        raise BadCode("That code is not right. Check the app and try again.")
    user.totp_enabled = True
    user.totp_last_step = step
    codes = _new_recovery_codes(db, user)
    db.flush()
    return codes


def disable(db: Session, user: User) -> None:
    user.totp_enabled = False
    user.totp_secret_enc = None
    user.totp_last_step = None
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    db.execute(delete(LoginChallenge).where(LoginChallenge.user_id == user.id))
    db.flush()


def regenerate_recovery_codes(db: Session, user: User) -> list[str]:
    if not user.totp_enabled:
        raise NotEnabled("Two-factor authentication is not on.")
    codes = _new_recovery_codes(db, user)
    db.flush()
    return codes


# --------------------------------------------------------------------------
# Recovery codes
# --------------------------------------------------------------------------


def _normalize_recovery(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def _new_recovery_codes(db: Session, user: User) -> list[str]:
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    codes: list[str] = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(12))
        codes.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
        db.add(RecoveryCode(user_id=user.id, code_hash=hash_token(_normalize_recovery(raw))))
    return codes


def recovery_codes_remaining(db: Session, user: User) -> int:
    return db.execute(
        select(func.count(RecoveryCode.id)).where(
            RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None)
        )
    ).scalar_one()


def _use_recovery_code(db: Session, user: User, code: str) -> bool:
    digest = hash_token(_normalize_recovery(code))
    row = db.execute(
        select(RecoveryCode).where(
            RecoveryCode.user_id == user.id,
            RecoveryCode.code_hash == digest,
            RecoveryCode.used_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    row.used_at = _utcnow()
    return True


# --------------------------------------------------------------------------
# Checking a code at login
# --------------------------------------------------------------------------


def check_code(
    db: Session, user: User, box: SecretBox | None, code: str, *, allow_recovery: bool = True
) -> bool:
    """True when `code` is a current authenticator code (not used before) or,
    if allowed, an unused recovery code - which it then spends."""
    if not user.totp_enabled or not code:
        return False
    if totp.is_totp_shaped(code):
        secret = pending_secret(user, box)
        if secret is None:
            return False  # key lost or changed: fail closed
        step = totp.verify(secret, code, last_step=user.totp_last_step)
        if step is None:
            return False
        user.totp_last_step = step
        return True
    return allow_recovery and _use_recovery_code(db, user, code)


# --------------------------------------------------------------------------
# Login challenges
# --------------------------------------------------------------------------


def create_challenge(db: Session, user: User) -> str:
    """The secret the client sends back with the code. Expired challenges are
    swept here, so nothing else needs a cleanup job."""
    db.execute(delete(LoginChallenge).where(LoginChallenge.expires_at < _utcnow()))
    raw = generate_token()
    db.add(
        LoginChallenge(
            user_id=user.id,
            secret_hash=hash_token(raw),
            expires_at=_utcnow() + CHALLENGE_LIFETIME,
        )
    )
    db.flush()
    return raw


def complete_challenge(
    db: Session,
    raw_secret: str,
    code: str,
    box: SecretBox | None,
    *,
    username: str | None = None,
    allow_recovery: bool = True,
    lockout: auth_service.Lockout | None = None,
    ip_address: str | None = None,
) -> User | None:
    """The user this challenge was issued to, when `code` is right - and the
    challenge is then spent. `None` for an unknown, expired or spent challenge
    or a wrong code (which counts against the challenge; too many kill it).

    The caller must commit either way: a failed attempt is recorded.
    """
    if not raw_secret:
        return None
    challenge = db.execute(
        select(LoginChallenge).where(LoginChallenge.secret_hash == hash_token(raw_secret))
    ).scalar_one_or_none()
    if challenge is None or _as_utc(challenge.expires_at) <= _utcnow():
        return None
    user = challenge.user
    if username is not None and user.username.lower() != username.lower():
        return None
    if not user.is_active or not user.totp_enabled:
        db.delete(challenge)
        return None
    if auth_service.is_locked(user):
        return None
    if check_code(db, user, box, code, allow_recovery=allow_recovery):
        db.delete(challenge)
        return user
    auth_service.register_failure(db, user, lockout, ip_address=ip_address)
    challenge.failed_attempts += 1
    if challenge.failed_attempts >= MAX_FAILED_ATTEMPTS:
        db.delete(challenge)
    return None


# --------------------------------------------------------------------------
# Key rotation
# --------------------------------------------------------------------------


def rotate_secrets(db: Session, box: SecretBox) -> tuple[int, int]:
    """Re-encrypt every stored TOTP secret with the primary key; returns
    (rotated, unreadable)."""
    rotated = unreadable = 0
    for user in db.execute(select(User).where(User.totp_secret_enc.is_not(None))).scalars():
        if user.totp_secret_enc is None:
            continue
        token = box.rotate(user.totp_secret_enc)
        if token is None:
            unreadable += 1
            continue
        user.totp_secret_enc = token
        rotated += 1
    return rotated, unreadable
