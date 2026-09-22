"""A thin wrapper around ldap3 for one thing: turn a username/password into a
directory entry that was actually proved (a real bind), or nothing. Never
logs a bind password or a user's password (CLAUDE.md section 21).

`server` and `client_strategy` are accepted so tests can point at an in-memory
ldap3 MOCK_SYNC directory instead of a real one; production callers omit both
and get a fresh real connection built from `config.server_url`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import ldap3
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

logger = logging.getLogger("rustdesk_api.ldap")

_ClientStrategy = Literal[
    "SYNC",
    "SAFE_RESTARTABLE",
    "SAFE_SYNC",
    "ASYNC",
    "LDIF",
    "RESTARTABLE",
    "REUSABLE",
    "MOCK_SYNC",
    "MOCK_ASYNC",
    "ASYNC_STREAM",
]
_AutoBind = Literal["DEFAULT", "NONE", "NO_TLS", "TLS_BEFORE_BIND", "TLS_AFTER_BIND"]


@dataclass(frozen=True)
class LdapConfig:
    server_url: str
    use_starttls: bool
    bind_dn: str
    bind_password: str
    base_dn: str
    user_search_filter: str  # contains a {username} placeholder
    username_attribute: str
    email_attribute: str
    display_name_attribute: str
    admin_group_dn: str
    link_by_email: bool
    auto_create_users: bool
    timeout_seconds: float


@dataclass(frozen=True)
class LdapUser:
    dn: str
    username: str
    email: str | None
    display_name: str | None
    is_in_admin_group: bool


def _server(config: LdapConfig) -> ldap3.Server:
    return ldap3.Server(config.server_url, connect_timeout=config.timeout_seconds)


def _auto_bind(config: LdapConfig) -> _AutoBind:
    return "TLS_BEFORE_BIND" if config.use_starttls else "NO_TLS"


def _attr(entry: ldap3.Entry, name: str) -> str | None:
    if name not in entry:
        return None
    values = entry[name].values
    return str(values[0]) if values else None


def search_user(
    config: LdapConfig,
    username: str,
    *,
    server: ldap3.Server | None = None,
    client_strategy: _ClientStrategy = "SYNC",
) -> LdapUser | None:
    """The directory entry for `username`, found with the service account bind.
    Never raises: a connection or search failure is logged and treated as "not
    found" so a caller can reject or fall back to another sign-in method."""
    attrs = [config.username_attribute, config.email_attribute, config.display_name_attribute, "memberOf"]
    try:
        with ldap3.Connection(
            server or _server(config),
            user=config.bind_dn or None,
            password=config.bind_password or None,
            auto_bind=_auto_bind(config),
            receive_timeout=config.timeout_seconds,
            client_strategy=client_strategy,
        ) as conn:
            filter_ = config.user_search_filter.format(username=escape_filter_chars(username))
            conn.search(config.base_dn, filter_, attributes=attrs)
            if not conn.entries:
                return None
            entry = conn.entries[0]
            member_of = [str(v) for v in entry["memberOf"].values] if "memberOf" in entry else []
            is_admin_member = bool(config.admin_group_dn) and any(
                dn.lower() == config.admin_group_dn.lower() for dn in member_of
            )
            return LdapUser(
                dn=str(entry.entry_dn),
                username=_attr(entry, config.username_attribute) or username,
                email=_attr(entry, config.email_attribute),
                display_name=_attr(entry, config.display_name_attribute),
                is_in_admin_group=is_admin_member,
            )
    except LDAPException:
        logger.warning("LDAP search for a user failed", exc_info=True)
        return None


def verify_password(
    config: LdapConfig,
    dn: str,
    password: str,
    *,
    server: ldap3.Server | None = None,
    client_strategy: _ClientStrategy = "SYNC",
) -> bool:
    """Binds as `dn` with `password`: the only real proof of the password.
    Never raises, never logs the password. Refuses an empty password outright
    (some directories treat it as an anonymous bind, which would "succeed")."""
    if not password:
        return False
    try:
        with ldap3.Connection(
            server or _server(config),
            user=dn,
            password=password,
            auto_bind=_auto_bind(config),
            receive_timeout=config.timeout_seconds,
            client_strategy=client_strategy,
        ) as conn:
            return bool(conn.bound)
    except LDAPException:
        return False
