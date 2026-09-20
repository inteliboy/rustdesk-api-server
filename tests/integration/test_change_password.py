"""A signed-in user changing their own password (POST /api/v1/auth/change-password)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rustdesk_api.config import clear_settings_cache

OLD = "alicepassword1"
NEW = "a-brand-new-pass-2"


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("LOGIN_LOCKOUT_THRESHOLD", "3")
    monkeypatch.setenv("LOGIN_LOCKOUT_MINUTES", "15")
    monkeypatch.setenv("AUTH_RATE_LIMIT_ATTEMPTS", "1000")
    clear_settings_cache()


def _make_user(admin_client, name="alice", password=OLD) -> int:
    r = admin_client.post("/api/v1/users", json={"username": name, "password": password})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _login(app, username="alice", password=OLD):
    client = TestClient(app)
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if r.status_code == 200:
        client.headers.update({"X-CSRF-Token": client.cookies.get("rd_csrf")})
    return client, r


def _change(client, current=OLD, new=NEW):
    return client.post(
        "/api/v1/auth/change-password", json={"current_password": current, "new_password": new}
    )


def test_a_user_changes_their_password_and_this_session_stays(app, admin_client):
    _make_user(admin_client)
    here, _ = _login(app)

    assert _change(here).status_code == 204

    assert here.get("/api/v1/auth/me").status_code == 200
    assert _login(app, password=OLD)[1].status_code == 401
    assert _login(app, password=NEW)[1].status_code == 200


def test_it_ends_every_other_session_and_key_but_not_other_users(app, admin_client):
    _make_user(admin_client)
    _make_user(admin_client, "bob", "bobpassword123")
    here, _ = _login(app)
    phone, _ = _login(app)
    bob, _ = _login(app, "bob", "bobpassword123")
    key = here.post("/api/v1/api-keys", json={"label": "script", "scope": "full", "days": 30}).json()
    script = TestClient(app, headers={"Authorization": f"Bearer {key['token']}"})
    assert script.get("/api/v1/devices").status_code == 200

    assert _change(here).status_code == 204

    assert phone.get("/api/v1/auth/me").status_code == 401
    assert script.get("/api/v1/devices").status_code == 401
    assert here.get("/api/v1/auth/me").status_code == 200
    assert bob.get("/api/v1/auth/me").status_code == 200


def test_a_wrong_current_password_changes_nothing(app, admin_client):
    _make_user(admin_client)
    here, _ = _login(app)

    r = _change(here, current="not-the-password")
    assert (r.status_code, r.json()["error"]["code"]) == (403, "INVALID_CREDENTIALS")

    assert here.get("/api/v1/auth/me").status_code == 200  # still signed in: not a 401
    assert _login(app, password=OLD)[1].status_code == 200
    assert _login(app, password=NEW)[1].status_code == 401


def test_wrong_current_passwords_lock_the_account_like_wrong_sign_ins(app, admin_client):
    _make_user(admin_client)
    here, _ = _login(app)
    for _ in range(3):
        assert _change(here, current="not-the-password").status_code == 403

    locked = _change(here)  # the right password no longer helps
    assert (locked.status_code, locked.json()["error"]["code"]) == (429, "ACCOUNT_LOCKED")
    assert _login(app, password=OLD)[1].status_code == 429


def test_a_right_password_clears_earlier_failures(app, admin_client):
    _make_user(admin_client)
    here, _ = _login(app)
    for _ in range(2):
        assert _change(here, current="not-the-password").status_code == 403
    assert _change(here).status_code == 204
    # Two more wrong tries would have locked it had the count survived.
    for _ in range(2):
        assert _change(here, current="not-the-password", new="another-pass-3").status_code == 403
    assert _login(app, password=NEW)[1].status_code == 200


def test_the_new_password_must_be_valid_and_different(app, admin_client):
    _make_user(admin_client)
    here, _ = _login(app)

    assert _change(here, new="short").status_code == 422
    same = _change(here, new=OLD)
    assert (same.status_code, same.json()["error"]["code"]) == (422, "PASSWORD_UNCHANGED")
    assert _login(app, password=OLD)[1].status_code == 200


def test_it_needs_a_login_and_csrf_and_refuses_api_keys(app, admin_client):
    _make_user(admin_client)
    assert _change(TestClient(app)).status_code == 401

    here, _ = _login(app)
    key = here.post("/api/v1/api-keys", json={"label": "script", "scope": "full", "days": 30}).json()
    script = TestClient(app, headers={"Authorization": f"Bearer {key['token']}"})
    r = _change(script)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "API_KEY_NOT_ALLOWED")

    del here.headers["X-CSRF-Token"]
    assert _change(here).status_code == 403
    assert _login(app, password=OLD)[1].status_code == 200


def test_it_is_audited_without_any_password(app, admin_client):
    _make_user(admin_client)
    here, _ = _login(app)
    _change(here, current="not-the-password")
    assert _change(here).status_code == 204

    entries = [
        e
        for e in admin_client.get("/api/v1/admin/audit-logs").json()["items"]
        if e["action"] == "password_changed"
    ]
    assert sorted(e["result"] for e in entries) == ["failure", "success"]
    assert all(e["actor_username"] == "alice" for e in entries if "actor_username" in e)
    dump = str(entries)
    assert OLD not in dump and NEW not in dump and "not-the-password" not in dump
