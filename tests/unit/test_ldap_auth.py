"""LDAP/Active Directory sign-in, against an in-memory ldap3 MOCK_SYNC directory
(no real network, no external server needed - runs on Windows like every other
test here). Covers security/ldap.py's search+bind and services/ldap_auth.py's
resolve order (existing identity -> link by email -> auto-create -> reject)."""

from __future__ import annotations

import ldap3
import pytest

from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_session_factory
from rustdesk_api.security import ldap as ldap_security
from rustdesk_api.security.ldap import LdapConfig
from rustdesk_api.services import authentication as auth_service
from rustdesk_api.services import ldap_auth
from rustdesk_api.services.ldap_auth import LdapError

BASE_DN = "dc=example,dc=com"
USER_DN = "uid=jdoe,ou=users,dc=example,dc=com"
ADMIN_GROUP_DN = "cn=admins,dc=example,dc=com"


@pytest.fixture()
def directory() -> ldap3.Server:
    """One in-memory directory, shared by every connection made against this
    exact Server object (ldap3's MOCK_SYNC keeps its DIT on the Server, not the
    Connection) - a service account, a user with a password, and one admin-group
    member, set up through a bind of our own."""
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
    setup.strategy.add_entry(
        "uid=bsmith,ou=users,dc=example,dc=com",
        {
            "objectClass": "inetOrgPerson",
            "uid": "bsmith",
            "mail": "bsmith@example.com",
            "displayName": "Bob Smith",
            "userPassword": "hunter2pass",
        },
    )
    setup.bind()
    return server


def _config(**overrides) -> LdapConfig:
    values = dict(
        server_url="ldap://dc1.example.test",
        use_starttls=False,
        bind_dn="cn=svc,dc=example,dc=com",
        bind_password="svc-pw",
        base_dn=BASE_DN,
        user_search_filter="(uid={username})",
        username_attribute="uid",
        email_attribute="mail",
        display_name_attribute="displayName",
        admin_group_dn=ADMIN_GROUP_DN,
        link_by_email=False,
        auto_create_users=False,
        timeout_seconds=5.0,
    )
    values.update(overrides)
    return LdapConfig(**values)


def _search(config: LdapConfig, username: str, server: ldap3.Server):
    return ldap_security.search_user(config, username, server=server, client_strategy=ldap3.MOCK_SYNC)


def _verify(config: LdapConfig, dn: str, password: str, server: ldap3.Server) -> bool:
    return ldap_security.verify_password(config, dn, password, server=server, client_strategy=ldap3.MOCK_SYNC)


# --- security/ldap.py: search + bind -----------------------------------------


def test_search_user_finds_a_known_user_with_its_attributes(directory):
    found = _search(_config(), "jdoe", directory)
    assert found is not None
    assert found.dn == USER_DN
    assert found.username == "jdoe"
    assert found.email == "jdoe@example.com"
    assert found.display_name == "Jane Doe"
    assert found.is_in_admin_group is True


def test_search_user_reports_no_admin_group_membership_for_a_plain_user(directory):
    found = _search(_config(), "bsmith", directory)
    assert found is not None
    assert found.is_in_admin_group is False


def test_search_user_returns_none_for_an_unknown_username(directory):
    assert _search(_config(), "nobody", directory) is None


def test_search_user_returns_none_when_the_service_bind_is_wrong(directory):
    assert _search(_config(bind_password="wrong"), "jdoe", directory) is None


def test_verify_password_succeeds_only_with_the_right_password(directory):
    config = _config()
    assert _verify(config, USER_DN, "secret123", directory) is True
    assert _verify(config, USER_DN, "wrong-password", directory) is False


def test_verify_password_refuses_an_empty_password(directory):
    """Some directories treat an empty password as an anonymous bind, which
    would otherwise "succeed" and let anyone in as anyone."""
    assert _verify(_config(), USER_DN, "", directory) is False


# --- services/ldap_auth.py: resolve_user (pure DB logic, no directory calls) -


def _ldap_user(directory, username: str) -> ldap_security.LdapUser:
    found = _search(_config(), username, directory)
    assert found is not None
    return found


def test_resolve_user_refuses_an_unlinked_account_by_default(app, directory):
    Session = get_session_factory()
    with Session() as db:
        with pytest.raises(LdapError):
            ldap_auth.resolve_user(db, _config(), _ldap_user(directory, "jdoe"))


def test_resolve_user_finds_the_user_behind_an_existing_identity(app, directory):
    Session = get_session_factory()
    with Session() as db:
        user = auth_service.create_user(db, username="local-jdoe", password="passwordpassword1")
        db.commit()
        ldap_auth._add_identity(db, user, _ldap_user(directory, "jdoe"))
        db.commit()

        found = ldap_auth.resolve_user(db, _config(), _ldap_user(directory, "jdoe"))
        assert found.id == user.id


def test_resolve_user_links_by_email_only_when_enabled(app, directory):
    Session = get_session_factory()
    with Session() as db:
        user = auth_service.create_user(
            db, username="local-jdoe", password="passwordpassword1", email="jdoe@example.com"
        )
        db.commit()

        with pytest.raises(LdapError):
            ldap_auth.resolve_user(db, _config(link_by_email=False), _ldap_user(directory, "jdoe"))

        found = ldap_auth.resolve_user(db, _config(link_by_email=True), _ldap_user(directory, "jdoe"))
        assert found.id == user.id


def test_resolve_user_applies_the_admin_group_mapping_on_every_login(app, directory):
    Session = get_session_factory()
    with Session() as db:
        user = auth_service.create_user(
            db, username="local-jdoe", password="passwordpassword1", email="jdoe@example.com"
        )
        db.commit()
        ldap_auth._add_identity(db, user, _ldap_user(directory, "jdoe"))
        db.commit()

        found = ldap_auth.resolve_user(db, _config(), _ldap_user(directory, "jdoe"))
        assert found.is_admin is True  # jdoe is a member of the configured admin group

        # bsmith (not in the admin group) must not become an admin just by having
        # the mapping configured.
        other = auth_service.create_user(
            db, username="local-bsmith", password="passwordpassword1", email="bsmith@example.com"
        )
        db.commit()
        ldap_auth._add_identity(db, other, _ldap_user(directory, "bsmith"))
        db.commit()
        found_other = ldap_auth.resolve_user(db, _config(), _ldap_user(directory, "bsmith"))
        assert found_other.is_admin is False


# --- services/ldap_auth.py: authenticate_or_provision (the full path) --------


def _settings(**changes) -> Settings:
    base = dict(
        ldap_enabled=True,
        ldap_server_url="ldap://dc1.example.test",
        ldap_base_dn=BASE_DN,
        ldap_bind_dn="cn=svc,dc=example,dc=com",
        ldap_bind_password="svc-pw",
        ldap_admin_group_dn=ADMIN_GROUP_DN,
    )
    base.update(changes)
    return Settings.model_construct().model_copy(update=base)


def _patch_directory(monkeypatch, directory) -> None:
    """Redirects the module's calls into ldap3 to the shared mock directory,
    the same boundary a real deployment crosses to reach an actual server.
    The real functions are captured before patching - `ldap_auth.ldap` and
    `ldap_security` are the same module object, so patching one is visible
    through the other, and a lambda that looked them up by name again would
    call itself."""
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


def test_authenticate_or_provision_returns_none_when_ldap_is_off(app, directory, monkeypatch):
    _patch_directory(monkeypatch, directory)
    Session = get_session_factory()
    with Session() as db:
        assert (
            ldap_auth.authenticate_or_provision(db, _settings(ldap_enabled=False), "jdoe", "secret123")
            is None
        )


def test_authenticate_or_provision_returns_none_for_a_wrong_password(app, directory, monkeypatch):
    _patch_directory(monkeypatch, directory)
    Session = get_session_factory()
    with Session() as db:
        assert ldap_auth.authenticate_or_provision(db, _settings(), "jdoe", "wrong") is None


def test_authenticate_or_provision_returns_none_for_an_unknown_username(app, directory, monkeypatch):
    _patch_directory(monkeypatch, directory)
    Session = get_session_factory()
    with Session() as db:
        assert ldap_auth.authenticate_or_provision(db, _settings(), "nobody", "whatever") is None


def test_authenticate_or_provision_refuses_to_create_a_user_when_off(app, directory, monkeypatch):
    _patch_directory(monkeypatch, directory)
    Session = get_session_factory()
    with Session() as db:
        assert (
            ldap_auth.authenticate_or_provision(
                db, _settings(ldap_auto_create_users=False), "jdoe", "secret123"
            )
            is None
        )


def test_authenticate_or_provision_creates_and_links_a_new_user(app, directory, monkeypatch):
    _patch_directory(monkeypatch, directory)
    Session = get_session_factory()
    with Session() as db:
        user = ldap_auth.authenticate_or_provision(
            db, _settings(ldap_auto_create_users=True), "jdoe", "secret123"
        )
        db.commit()
        assert user is not None
        assert user.username == "jdoe"
        assert user.email == "jdoe@example.com"
        assert user.is_admin is True  # a member of the configured admin group
        assert auth_service.get_user_by_username(db, "jdoe").is_active is True

        # Signing in again finds the same local user through the identity just made.
        again = ldap_auth.authenticate_or_provision(
            db, _settings(ldap_auto_create_users=True), "jdoe", "secret123"
        )
        assert again is not None
        assert again.id == user.id
