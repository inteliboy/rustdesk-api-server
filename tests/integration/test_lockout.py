"""Account lockout after repeated wrong passwords."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rustdesk_api.config import clear_settings_cache

PASSWORD = "alicepassword1"


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("LOGIN_LOCKOUT_THRESHOLD", "3")
    monkeypatch.setenv("LOGIN_LOCKOUT_MINUTES", "15")
    monkeypatch.setenv("AUTH_RATE_LIMIT_ATTEMPTS", "1000")
    clear_settings_cache()


def _make_alice(admin_client) -> int:
    r = admin_client.post("/api/v1/users", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _login(app, password, username="alice"):
    return TestClient(app).post("/api/v1/auth/login", json={"username": username, "password": password})


def _client_login(app, password, username="alice"):
    return TestClient(app).post("/api/login", json={"username": username, "password": password}).json()


def test_three_wrong_passwords_lock_the_account_even_for_the_right_one(app, admin_client):
    _make_alice(admin_client)
    for _ in range(3):
        assert _login(app, "wrong-password").status_code == 401

    locked = _login(app, PASSWORD)
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "ACCOUNT_LOCKED"
    assert "minute" in locked.json()["error"]["message"]


def test_the_lock_applies_to_the_rustdesk_client_login_too(app, admin_client):
    _make_alice(admin_client)
    for _ in range(3):
        assert "error" in _client_login(app, "wrong-password")
    body = _client_login(app, PASSWORD)
    assert "access_token" not in body
    assert "Too many" in body["error"]


def test_a_successful_login_resets_the_count(app, admin_client):
    _make_alice(admin_client)
    for _ in range(2):
        _login(app, "wrong-password")
    assert _login(app, PASSWORD).status_code == 200
    for _ in range(2):
        _login(app, "wrong-password")
    assert _login(app, PASSWORD).status_code == 200  # 2 + 2 wrong, but never 3 in a row


def test_an_administrator_can_unlock(app, admin_client):
    alice_id = _make_alice(admin_client)
    for _ in range(3):
        _login(app, "wrong-password")
    listed = {u["username"]: u for u in admin_client.get("/api/v1/users").json()}
    assert listed["alice"]["locked_until"] is not None

    assert admin_client.post(f"/api/v1/users/{alice_id}/unlock").status_code == 200
    assert _login(app, PASSWORD).status_code == 200


def test_only_an_administrator_can_unlock(app, admin_client):
    alice_id = _make_alice(admin_client)
    for _ in range(3):
        _login(app, "wrong-password")
    bob = admin_client.post("/api/v1/users", json={"username": "bob", "password": "bobpassword12"})
    assert bob.status_code == 201
    other = TestClient(app)
    other.post("/api/v1/auth/login", json={"username": "bob", "password": "bobpassword12"})
    other.headers.update({"X-CSRF-Token": other.cookies.get("rd_csrf")})
    assert other.post(f"/api/v1/users/{alice_id}/unlock").status_code == 403
    assert _login(app, PASSWORD).status_code == 429


def test_locking_is_audited_and_an_unknown_user_is_not_locked_or_revealed(app, admin_client):
    _make_alice(admin_client)
    for _ in range(3):
        _login(app, "wrong-password")
    actions = [e["action"] for e in admin_client.get("/api/v1/admin/audit-logs").json()["items"]]
    assert "account_locked" in actions
    for _ in range(5):
        r = _login(app, "whatever", username="nobody")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_threshold_zero_turns_lockout_off(app, admin_client, monkeypatch):
    monkeypatch.setenv("LOGIN_LOCKOUT_THRESHOLD", "0")
    clear_settings_cache()
    from rustdesk_api.app import create_app

    app2 = create_app()
    _make_alice(admin_client)
    for _ in range(6):
        assert _login(app2, "wrong-password").status_code == 401
    assert _login(app2, PASSWORD).status_code == 200
