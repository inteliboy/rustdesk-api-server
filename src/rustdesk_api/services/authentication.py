from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rustdesk_api.config import Settings
from rustdesk_api.models.user import User
from rustdesk_api.security.passwords import hash_password, verify_password
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import notifications


class InvalidCredentials(Exception):
    pass


class AccountDisabled(Exception):
    pass


class AccountLocked(Exception):
    """Too many wrong passwords in a row; the account refuses every sign-in
    (right password included) until `locked_until`."""


@dataclass(frozen=True)
class Lockout:
    """How many wrong passwords lock an account, and for how long."""

    threshold: int = 0  # 0 = never lock
    minutes: int = 15

    @classmethod
    def from_settings(cls, settings: Settings) -> Lockout:
        return cls(settings.login_lockout_threshold, settings.login_lockout_minutes)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def is_locked(user: User, now: datetime.datetime | None = None) -> bool:
    return user.locked_until is not None and _as_utc(user.locked_until) > (now or _utcnow())


def lock_message(user: User) -> str:
    assert user.locked_until is not None
    seconds = (_as_utc(user.locked_until) - _utcnow()).total_seconds()
    minutes = max(1, int(seconds // 60) + 1)
    return f"Too many failed sign-in attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}."


def register_failure(
    db: Session, user: User, policy: Lockout | None, *, ip_address: str | None = None
) -> bool:
    """Count a wrong password (or a wrong second factor). True when this one
    locked the account. The counter is not reset when a lock lapses, so the
    first wrong guess after it locks again: guessing stays slow."""
    user.failed_logins += 1
    if policy is None or policy.threshold <= 0 or user.failed_logins < policy.threshold:
        return False
    if is_locked(user):
        return False
    user.locked_until = _utcnow() + datetime.timedelta(minutes=policy.minutes)
    audit_service.record(
        db,
        action="account_locked",
        target_type="user",
        target_id=user.id,
        result="failure",
        ip_address=ip_address,
        detail={"username": user.username, "failed_logins": user.failed_logins, "minutes": policy.minutes},
    )
    notifications.dispatch(
        "account_locked",
        "Account locked",
        f'The account "{user.username}" was locked for {policy.minutes} minute(s) after '
        f"{user.failed_logins} wrong passwords"
        + (f" (last attempt from {ip_address})." if ip_address else "."),
        data={"username": user.username, "failed_logins": user.failed_logins, "ip": ip_address},
        dedupe_key=user.username,
        throttle_seconds=300,
    )
    return True


def register_success(user: User) -> None:
    user.failed_logins = 0
    user.locked_until = None


def unlock(user: User) -> None:
    register_success(user)


def any_users_exist(db: Session) -> bool:
    return db.execute(select(func.count(User.id))).scalar_one() > 0


def get_user_by_username(db: Session, username: str) -> User | None:
    stmt = select(User).where(func.lower(User.username) == username.lower())
    return db.execute(stmt).scalar_one_or_none()


def get_user_for_login(db: Session, identifier: str) -> User | None:
    """The account a sign-in name refers to: the username, else (when it looks
    like an address) the e-mail. A username wins over another account's e-mail,
    so nobody can take over a name by registering it as their address."""
    identifier = identifier.strip()
    user = get_user_by_username(db, identifier)
    if user is not None or "@" not in identifier:
        return user
    stmt = select(User).where(func.lower(User.email) == identifier.lower()).limit(2)
    matches = db.execute(stmt).scalars().all()
    # Two accounts whose addresses differ only by case: sign in by username.
    return matches[0] if len(matches) == 1 else None


def create_user(
    db: Session,
    *,
    username: str,
    password: str,
    email: str | None = None,
    is_admin: bool = False,
) -> User:
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        is_admin=is_admin,
        is_active=True,
    )
    db.add(user)
    db.flush()
    return user


def authenticate(
    db: Session,
    *,
    username: str,
    password: str,
    lockout: Lockout | None = None,
    ip_address: str | None = None,
) -> User:
    """Raises InvalidCredentials, AccountDisabled or AccountLocked; never
    returns None, so callers can't accidentally treat a falsy return as
    success/failure ambiguously. A wrong password is counted against the
    account, so the caller must commit after catching InvalidCredentials.

    A correct password does not reset the failure count: with 2FA the login
    is not finished yet. The caller calls `register_success` once it is.
    """
    user = get_user_for_login(db, username)
    if user is None:
        # Perform a dummy hash verification so that responses for unknown
        # usernames take a similar amount of time to known ones.
        dummy_hash = (
            "$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$"
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )
        verify_password(password, dummy_hash)
        raise InvalidCredentials("Invalid username or password.")

    password_ok = verify_password(password, user.password_hash)
    if is_locked(user):
        raise AccountLocked(lock_message(user))
    if not password_ok:
        register_failure(db, user, lockout, ip_address=ip_address)
        raise InvalidCredentials("Invalid username or password.")

    if not user.is_active:
        raise AccountDisabled("This account has been deactivated.")

    user.last_login_at = _utcnow()
    return user


def set_password(db: Session, user: User, new_password: str) -> None:
    user.password_hash = hash_password(new_password)
