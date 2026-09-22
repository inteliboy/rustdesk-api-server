"""LDAP sign-in through the real WebUI login endpoint (/api/v1/auth/login): a
password that does not match locally falls back to a directory search+bind
(api/auth.py:webui_login, services/ldap_auth.py), against an in-memory ldap3
MOCK_SYNC directory - no real network, no external server.
"""

from __future__ import annotations

import ldap3
from fastapi.testclient import TestClient

from rustdesk_api.config import clear_settings_cache
from rustdesk_api.security import ldap as ldap_security
from rustdesk_api.services import ldap_auth

BASE_DN = "dc=example,dc=com"
USER_DN = "uid=jdoe,ou=users,dc=example,dc=com"
ADMIN_GROUP_DN = "cn=admins,dc=example,dc=com"


def _directory() -> ldap3.Server:
    server = ldap3.Server("dc1.example.test")
    setup = ldap3.Connection(server, client_strategy=ldap3.MOCK_SYNC)
    setup.open()
    setup.strategy.add_entry("cn=svc,dc=example,dc=com", {"userPassword": "svc-pw"})
    setup.strategy.add_entry(
        USER_DN,
        {
            "objectClass": "inetOrgPerson",
            "uid": "jdoe",
            "mail": "jdoe@example.com",
            "displayName": "Jane Doe",
            "userPassword": "secret123",
            "memberOf": [ADMIN_GROUP_DN],
        },
    )
    setup.bind()
    return server


def _patch_directory(monkeypatch, directory: ldap3.Server) -> None:
    real_search, real_verify = ldap_security.search_user, ldap_security.verify_password
    monkeypatch.setattr(
        ldap_auth.ldap,
        "search_user",
        lambda config, username: real_search(
            config, username, server=directory, client_strategy=ldap3.MOCK_SYNC
        ),
    )
    monkeypatch.setattr(
        ldap_auth.ldap,
        "verify_password",
        lambda config, dn, password: real_verify(
            config, dn, password, server=directory, client_strategy=ldap3.MOCK_SYNC
        ),
    )


def _enable_ldap(monkeypatch, **extra: str) -> None:
    """Turns LDAP on for the *next* request: get_settings_dep() (api/deps.py)
    re-reads Settings per-request, so an app already built by the `app`
    fixture picks this up without needing a second one built here."""
    env = {
        "LDAP_ENABLED": "true",
        "LDAP_SERVER_URL": "ldap://dc1.example.test",
        "LDAP_BASE_DN": BASE_DN,
        "LDAP_BIND_DN": "cn=svc,dc=example,dc=com",
        "LDAP_BIND_PASSWORD": "svc-pw",
        "LDAP_AUTO_CREATE_USERS": "true",
        "LDAP_ADMIN_GROUP_DN": ADMIN_GROUP_DN,
        **extra,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    clear_settings_cache()


def test_a_password_that_matches_no_local_user_is_tried_against_ldap(app, monkeypatch):
    directory = _directory()
    _patch_directory(monkeypatch, directory)
    _enable_ldap(monkeypatch)

    with TestClient(app) as client:
        r = client.post("/api/v1/auth/login", json={"username": "jdoe", "password": "secret123"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"]["username"] == "jdoe"
        # A member of the configured admin group, mapped on sign-in.
        assert body["user"]["is_admin"] is True
        assert body["user"]["has_password"] is False  # provisioned by LDAP, no local password

        # Signing in again finds the same account through the identity just made.
        client.post("/api/v1/auth/logout")
        r = client.post("/api/v1/auth/login", json={"username": "jdoe", "password": "secret123"})
        assert r.status_code == 200
        assert r.json()["user"]["username"] == "jdoe"


def test_a_wrong_directory_password_is_an_ordinary_invalid_credentials_error(app, monkeypatch):
    directory = _directory()
    _patch_directory(monkeypatch, directory)
    _enable_ldap(monkeypatch)

    with TestClient(app) as client:
        r = client.post("/api/v1/auth/login", json={"username": "jdoe", "password": "wrong"})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_ldap_is_never_tried_for_a_locked_local_account(app, monkeypatch):
    """A local account locked out by repeated wrong passwords must not be
    reachable through LDAP - only InvalidCredentials falls back, never
    AccountLocked/AccountDisabled."""
    directory = _directory()
    _patch_directory(monkeypatch, directory)
    _enable_ldap(monkeypatch, LOGIN_LOCKOUT_THRESHOLD="1", LOGIN_LOCKOUT_MINUTES="10")

    with TestClient(app) as client:
        setup = client.post(
            "/api/v1/auth/setup",
            json={"username": "jdoe", "password": "a-completely-different-password1"},
        )
        assert setup.status_code == 201, setup.text
        client.post("/api/v1/auth/logout")

        # One wrong local password locks the account (threshold 1).
        r = client.post("/api/v1/auth/login", json={"username": "jdoe", "password": "nope"})
        assert r.status_code in (401, 429)

        # Now even the *correct* LDAP password must not get in.
        r = client.post("/api/v1/auth/login", json={"username": "jdoe", "password": "secret123"})
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "ACCOUNT_LOCKED"


def test_repeated_successful_ldap_logins_never_lock_the_account(app, monkeypatch):
    """The local password hash never matches an LDAP-provisioned account, so
    every attempt would "fail" locally before the LDAP fallback runs; the
    fallback must undo that strike on success or the account locks itself out
    over time even though every directory password was correct."""
    directory = _directory()
    _patch_directory(monkeypatch, directory)
    _enable_ldap(monkeypatch, LOGIN_LOCKOUT_THRESHOLD="3", LOGIN_LOCKOUT_MINUTES="10")

    with TestClient(app) as client:
        for _ in range(5):
            r = client.post("/api/v1/auth/login", json={"username": "jdoe", "password": "secret123"})
            assert r.status_code == 200, r.text
            client.post("/api/v1/auth/logout")
