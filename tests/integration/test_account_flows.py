"""Self-registration, administrator-issued password-reset links, and the user's
own login sessions."""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from rustdesk_api.config import clear_settings_cache
from rustdesk_api.db.database import get_session_factory


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("AUTH_RATE_LIMIT_ATTEMPTS", "1000")
    clear_settings_cache()


def _register(client, username="newbie", password="newbiepassword1", **extra):
    return client.post("/api/v1/auth/register", json={"username": username, "password": password, **extra})


def _login(app, username, password):
    client = TestClient(app)
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if r.status_code == 200:
        client.headers.update({"X-CSRF-Token": client.cookies.get("rd_csrf")})
    return client, r


def _enable_registration(monkeypatch, approval=True):
    monkeypatch.setenv("ALLOW_REGISTRATION", "true")
    monkeypatch.setenv("REGISTRATION_REQUIRES_APPROVAL", "true" if approval else "false")
    clear_settings_cache()
    from rustdesk_api.app import create_app

    return create_app()


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def test_registration_is_off_by_default(admin_client):
    assert admin_client.get("/api/v1/auth/options").json()["registration_enabled"] is False
    r = _register(admin_client)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "REGISTRATION_DISABLED")


def test_registration_needs_the_first_administrator_to_exist(client, monkeypatch):
    app = _enable_registration(monkeypatch)
    c = TestClient(app)
    assert c.get("/api/v1/auth/options").json()["registration_enabled"] is False
    assert _register(c).status_code == 403  # /setup is the way in on an empty server


def test_a_registered_account_waits_for_approval_and_is_never_an_admin(admin_client, monkeypatch):
    app = _enable_registration(monkeypatch)
    c = TestClient(app)
    assert c.get("/api/v1/auth/options").json() == {
        "registration_enabled": True,
        "registration_requires_approval": True,
        "oidc_name": None,
    }
    r = _register(c, is_admin=True)
    assert (r.status_code, r.json()) == (201, {"status": "pending_approval"})

    _, denied = _login(app, "newbie", "newbiepassword1")
    assert denied.status_code == 401  # deactivated

    users = {u["username"]: u for u in admin_client.get("/api/v1/users").json()}
    assert users["newbie"]["is_admin"] is False and users["newbie"]["is_active"] is False
    activated = admin_client.patch(f"/api/v1/users/{users['newbie']['id']}", json={"is_active": True})
    assert activated.status_code == 200
    _, ok = _login(app, "newbie", "newbiepassword1")
    assert ok.status_code == 200


def test_registration_without_approval_can_sign_in_at_once(admin_client, monkeypatch):
    app = _enable_registration(monkeypatch, approval=False)
    assert _register(TestClient(app)).json() == {"status": "active"}
    assert _login(app, "newbie", "newbiepassword1")[1].status_code == 200


def test_registration_rejects_duplicates_and_records_an_audit_entry(admin_client, monkeypatch):
    app = _enable_registration(monkeypatch)
    c = TestClient(app)
    assert _register(c, email="n@example.com").status_code == 201
    assert _register(c).json()["error"]["code"] == "USERNAME_TAKEN"
    dup = _register(c, username="someone-else", email="N@example.com")
    assert dup.json()["error"]["code"] == "EMAIL_TAKEN"
    actions = [e["action"] for e in admin_client.get("/api/v1/admin/audit-logs").json()["items"]]
    assert "user_registered" in actions


# --------------------------------------------------------------------------
# Password reset links
# --------------------------------------------------------------------------


def _issue(admin_client, user_id):
    r = admin_client.post(f"/api/v1/users/{user_id}/reset-link")
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert "#token=" in url  # in the fragment: never sent to the server
    return url.split("#token=", 1)[1]


def _make_user(admin_client, name="alice", password="alicepassword1"):
    r = admin_client.post("/api/v1/users", json={"username": name, "password": password})
    assert r.status_code == 201
    return r.json()["id"]


def _redeem(client, token, password="brandnewpass1"):
    return client.post("/api/v1/auth/reset-password", json={"token": token, "password": password})


def test_a_reset_link_sets_a_new_password_once_and_ends_old_sessions(app, admin_client):
    alice_id = _make_user(admin_client)
    old_session, _ = _login(app, "alice", "alicepassword1")
    assert old_session.get("/api/v1/auth/me").status_code == 200

    token = _issue(admin_client, alice_id)
    anon = TestClient(app)
    assert _redeem(anon, token).status_code == 204

    assert old_session.get("/api/v1/auth/me").status_code == 401
    assert _login(app, "alice", "alicepassword1")[1].status_code == 401
    assert _login(app, "alice", "brandnewpass1")[1].status_code == 200

    again = _redeem(anon, token, "another-pass-1")
    assert (again.status_code, again.json()["error"]["code"]) == (400, "INVALID_RESET_LINK")


def test_a_new_link_voids_the_old_one_and_garbage_is_refused(app, admin_client):
    alice_id = _make_user(admin_client)
    first = _issue(admin_client, alice_id)
    second = _issue(admin_client, alice_id)
    anon = TestClient(app)
    assert _redeem(anon, first).status_code == 400
    assert _redeem(anon, "nonsense").status_code == 400
    assert _redeem(anon, second).status_code == 204


def test_an_expired_link_is_refused(app, admin_client):
    alice_id = _make_user(admin_client)
    token = _issue(admin_client, alice_id)
    past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).isoformat(sep=" ")
    with get_session_factory()() as db:
        db.execute(text("UPDATE password_reset_tokens SET expires_at = :t"), {"t": past})
        db.commit()
    assert _redeem(TestClient(app), token).status_code == 400


def test_a_reset_lifts_a_lockout(app, admin_client, monkeypatch):
    monkeypatch.setenv("LOGIN_LOCKOUT_THRESHOLD", "2")
    clear_settings_cache()
    from rustdesk_api.app import create_app

    app2 = create_app()
    alice_id = _make_user(admin_client)
    for _ in range(2):
        _login(app2, "alice", "wrong-password")
    assert _login(app2, "alice", "alicepassword1")[1].status_code == 429
    token = _issue(admin_client, alice_id)
    assert _redeem(TestClient(app2), token).status_code == 204
    assert _login(app2, "alice", "brandnewpass1")[1].status_code == 200


def test_only_an_administrator_can_issue_a_link_and_a_disabled_user_gets_none(app, admin_client):
    alice_id = _make_user(admin_client)
    bob_id = _make_user(admin_client, "bob", "bobpassword123")
    bob, _ = _login(app, "bob", "bobpassword123")
    assert bob.post(f"/api/v1/users/{alice_id}/reset-link").status_code == 403

    admin_client.patch(f"/api/v1/users/{bob_id}", json={"is_active": False})
    assert admin_client.post(f"/api/v1/users/{bob_id}/reset-link").status_code == 409
    assert admin_client.post("/api/v1/users/9999/reset-link").status_code == 404


# --------------------------------------------------------------------------
# Login sessions
# --------------------------------------------------------------------------


def test_a_user_sees_and_revokes_only_their_own_sessions(app, admin_client):
    _make_user(admin_client)
    _make_user(admin_client, "bob", "bobpassword123")
    laptop, _ = _login(app, "alice", "alicepassword1")
    phone, _ = _login(app, "alice", "alicepassword1")
    bob, _ = _login(app, "bob", "bobpassword123")

    mine = laptop.get("/api/v1/auth/sessions").json()
    assert len(mine) == 2
    assert [s["current"] for s in mine].count(True) == 1
    other = next(s for s in mine if not s["current"])

    # Bob cannot touch the session, and cannot tell it exists.
    assert bob.delete(f"/api/v1/auth/sessions/{other['id']}").status_code == 404
    assert phone.get("/api/v1/auth/me").status_code == 200

    assert laptop.delete(f"/api/v1/auth/sessions/{other['id']}").status_code == 204
    assert phone.get("/api/v1/auth/me").status_code == 401
    assert len(laptop.get("/api/v1/auth/sessions").json()) == 1
    assert len(bob.get("/api/v1/auth/sessions").json()) == 1


def test_sign_out_everywhere_else_keeps_this_session(app, admin_client):
    _make_user(admin_client)
    keep, _ = _login(app, "alice", "alicepassword1")
    others = [_login(app, "alice", "alicepassword1")[0] for _ in range(2)]

    r = keep.post("/api/v1/auth/sessions/revoke-others")
    assert (r.status_code, r.json()) == (200, {"revoked": 2})
    assert keep.get("/api/v1/auth/me").status_code == 200
    assert all(o.get("/api/v1/auth/me").status_code == 401 for o in others)


def test_sessions_need_a_login_and_a_cookie_session_needs_csrf(app, admin_client):
    _make_user(admin_client)
    assert TestClient(app).get("/api/v1/auth/sessions").status_code == 401
    alice, _ = _login(app, "alice", "alicepassword1")
    del alice.headers["X-CSRF-Token"]
    assert alice.post("/api/v1/auth/sessions/revoke-others").status_code == 403
