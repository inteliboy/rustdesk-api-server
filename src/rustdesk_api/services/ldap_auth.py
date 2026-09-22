"""LDAP/Active Directory sign-in: deciding which local user a directory
account is, modelled on services/oidc.py's resolve_user but simpler - there is
only ever one configured directory, so a username alone (not an issuer+subject
pair) identifies the remote account, and there is no browser redirect leg: a
sign-in is a direct search + bind, checked from api/auth.py's normal password
login when the password does not match locally.

Who an LDAP account is (`resolve_user`), in this order and nothing else:

1. The linked LdapIdentity for this username.
2. Only when LDAP_LINK_BY_EMAIL is on: a local user with the same e-mail address.
3. Only when LDAP_AUTO_CREATE_USERS is on: a new, non-admin user.

Otherwise the sign-in is refused. `authenticate_or_provision` never raises: a
disabled directory, an unreachable one, or a wrong password all come back as
None, so the caller can fall back to (or report) an ordinary invalid-credentials
failure without treating LDAP as the reason.
"""

from __future__ import annotations

import datetime
import re
import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rustdesk_api.config import Settings
from rustdesk_api.models.ldap_identity import LdapIdentity
from rustdesk_api.models.user import User
from rustdesk_api.security import ldap
from rustdesk_api.security.ldap import LdapConfig, LdapUser
from rustdesk_api.services import authentication as auth_service
from rustdesk_api.services.oidc import UNUSABLE_PASSWORD, has_usable_password

__all__ = [
    "UNUSABLE_PASSWORD",
    "has_usable_password",
    "config_from_settings",
    "resolve_user",
    "authenticate_or_provision",
]


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class LdapError(Exception):
    """A sign-in that cannot go on. `public` is safe to show the user; `reason`
    is a short code for the audit log. Neither ever contains a password."""

    def __init__(self, public: str, reason: str) -> None:
        super().__init__(reason)
        self.public = public
        self.reason = reason


def config_from_settings(settings: Settings) -> LdapConfig | None:
    """The configured directory, or None when LDAP is off."""
    if not settings.ldap_enabled or not settings.ldap_server_url or not settings.ldap_base_dn:
        return None
    return LdapConfig(
        server_url=settings.ldap_server_url,
        use_starttls=settings.ldap_use_starttls,
        bind_dn=settings.ldap_bind_dn,
        bind_password=settings.ldap_bind_password,
        base_dn=settings.ldap_base_dn,
        user_search_filter=settings.ldap_user_search_filter,
        username_attribute=settings.ldap_username_attribute,
        email_attribute=settings.ldap_email_attribute,
        display_name_attribute=settings.ldap_display_name_attribute,
        admin_group_dn=settings.ldap_admin_group_dn,
        link_by_email=settings.ldap_link_by_email,
        auto_create_users=settings.ldap_auto_create_users,
        timeout_seconds=settings.ldap_timeout_seconds,
    )


def _identity_of_username(db: Session, ldap_username: str) -> LdapIdentity | None:
    return db.execute(
        select(LdapIdentity).where(func.lower(LdapIdentity.ldap_username) == ldap_username.lower())
    ).scalar_one_or_none()


def _free_username(db: Session, ldap_user: LdapUser) -> str:
    base = re.sub(r"[^A-Za-z0-9._@-]", "", ldap_user.username)[:100] or f"ldap-{secrets.token_hex(4)}"
    name, n = base, 1
    while auth_service.get_user_by_username(db, name) is not None:
        n += 1
        name = f"{base}-{n}"
    return name


def resolve_user(db: Session, config: LdapConfig, ldap_user: LdapUser) -> User:
    """The local user for a directory account that has already proved its
    password (security.ldap.verify_password). Raises LdapError if it cannot
    be linked to one - the caller (authenticate_or_provision) is the only
    place a new user is created, when LDAP_AUTO_CREATE_USERS allows it."""
    identity = _identity_of_username(db, ldap_user.username)
    if identity is not None:
        user = db.get(User, identity.user_id)
        if user is None:
            raise LdapError("No account is linked to this sign-in.", "identity_orphaned")
        identity.last_login_at = _utcnow()
        _apply_admin_group(config, ldap_user, user)
        return user

    if config.link_by_email and ldap_user.email:
        existing = db.execute(
            select(User).where(func.lower(User.email) == ldap_user.email.lower())
        ).scalar_one_or_none()
        if existing is not None and _identity_of_user(db, existing.id) is None:
            _add_identity(db, existing, ldap_user)
            _apply_admin_group(config, ldap_user, existing)
            return existing

    raise LdapError("No account is linked to this directory sign-in.", "not_linked")


def _identity_of_user(db: Session, user_id: int) -> LdapIdentity | None:
    return db.execute(select(LdapIdentity).where(LdapIdentity.user_id == user_id)).scalar_one_or_none()


def _add_identity(db: Session, user: User, ldap_user: LdapUser) -> None:
    now = _utcnow()
    db.add(
        LdapIdentity(
            user_id=user.id,
            ldap_username=ldap_user.username.lower(),
            distinguished_name=ldap_user.dn,
            created_at=now,
            last_login_at=now,
        )
    )
    db.flush()


def _apply_admin_group(config: LdapConfig, ldap_user: LdapUser, user: User) -> None:
    if config.admin_group_dn:
        user.is_admin = ldap_user.is_in_admin_group


def _create_and_link(db: Session, ldap_user: LdapUser) -> User:
    user = User(
        username=_free_username(db, ldap_user),
        email=ldap_user.email,
        password_hash=UNUSABLE_PASSWORD,
        is_admin=False,
        is_active=True,
    )
    db.add(user)
    db.flush()
    _add_identity(db, user, ldap_user)
    return user


def authenticate_or_provision(db: Session, settings: Settings, username: str, password: str) -> User | None:
    """Tries LDAP for `username`/`password`. Returns the resolved local user on
    success, or None for any reason (LDAP off, unreachable, wrong password, or
    an account that cannot be linked and LDAP_AUTO_CREATE_USERS is off) - never
    raises, so a caller can fall back to reporting ordinary invalid credentials."""
    config = config_from_settings(settings)
    if config is None:
        return None
    ldap_user = ldap.search_user(config, username)
    if ldap_user is None:
        return None
    if not ldap.verify_password(config, ldap_user.dn, password):
        return None
    try:
        user = resolve_user(db, config, ldap_user)
    except LdapError:
        if not config.auto_create_users:
            return None
        user = _create_and_link(db, ldap_user)
        _apply_admin_group(config, ldap_user, user)
    return user
