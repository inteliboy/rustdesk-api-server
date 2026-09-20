"""Personal API keys: bearer tokens for scripts calling /api/v1."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rustdesk_api.config import clear_settings_cache


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("AUTH_RATE_LIMIT_ATTEMPTS", "1000")
    clear_settings_cache()


def _key(client, label="ci", scope="read", days=30):
    r = client.post("/api/v1/api-keys", json={"label": label, "scope": scope, "days": days})
    assert r.status_code == 201, r.text
    return r.json()


def _script(app, token):
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def _user_client(app, admin_client, name="bob"):
    r = admin_client.post("/api/v1/users", json={"username": name, "password": "bobpassword123"})
    assert r.status_code == 201
    client = TestClient(app)
    client.post("/api/v1/auth/login", json={"username": name, "password": "bobpassword123"})
    client.headers.update({"X-CSRF-Token": client.cookies.get("rd_csrf")})
    return client, r.json()["id"]


def test_a_read_key_reads_but_cannot_change_anything(app, admin_client):
    key = _key(admin_client, scope="read")
    script = _script(app, key["token"])
    assert script.get("/api/v1/devices").status_code == 200
    assert script.get("/api/v1/auth/me").json()["username"] == "admin"

    blocked = script.post("/api/v1/tags", json={"name": "x", "color": "#112233"})
    assert (blocked.status_code, blocked.json()["error"]["code"]) == (403, "API_KEY_READ_ONLY")


def test_a_full_key_acts_as_its_owner(app, admin_client):
    script = _script(app, _key(admin_client, scope="full")["token"])
    assert script.post("/api/v1/tags", json={"name": "x", "color": "#112233"}).status_code == 201


def test_a_key_is_shown_once_and_only_a_hash_is_kept(app, admin_client):
    key = _key(admin_client)
    listed = admin_client.get("/api/v1/api-keys").json()
    assert [k["label"] for k in listed] == ["ci"]
    assert "token" not in listed[0]
    assert key["token"] not in admin_client.get("/api/v1/api-keys").text


def test_a_key_cannot_manage_the_account(app, admin_client):
    script = _script(app, _key(admin_client, scope="full")["token"])
    for method, path in (
        ("get", "/api/v1/api-keys"),
        ("post", "/api/v1/api-keys"),
        ("get", "/api/v1/auth/sessions"),
        ("post", "/api/v1/auth/2fa/setup"),
        ("get", "/api/v1/enrollment-tokens"),
    ):
        r = getattr(script, method)(path, **({"json": {"label": "x"}} if method == "post" else {}))
        assert r.status_code == 403, (path, r.text)
        assert r.json()["error"]["code"] == "API_KEY_NOT_ALLOWED"


def test_a_key_is_not_a_cookie_and_not_a_rustdesk_client_token(app, admin_client):
    token = _key(admin_client, scope="full")["token"]
    cookie_only = TestClient(app, cookies={"rd_session": token})
    assert cookie_only.get("/api/v1/devices").status_code == 401
    # The RustDesk client endpoints ignore API keys altogether.
    r = TestClient(app).post(
        "/api/currentUser", json={"id": "1", "uuid": "u"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.json() == {"error": "Not authenticated."}


def test_revoking_a_key_stops_it_and_only_the_owner_can_revoke(app, admin_client):
    bob, _ = _user_client(app, admin_client)
    key = _key(admin_client)
    script = _script(app, key["token"])
    assert script.get("/api/v1/devices").status_code == 200
    assert bob.delete(f"/api/v1/api-keys/{key['id']}").status_code == 404
    assert script.get("/api/v1/devices").status_code == 200

    assert admin_client.delete(f"/api/v1/api-keys/{key['id']}").status_code == 204
    assert script.get("/api/v1/devices").status_code == 401
    assert admin_client.get("/api/v1/api-keys").json() == []


def test_a_key_follows_its_users_account_and_is_bounded(app, admin_client):
    bob, bob_id = _user_client(app, admin_client)
    script = _script(app, _key(bob)["token"])
    assert script.get("/api/v1/devices").status_code == 200

    admin_client.patch(f"/api/v1/users/{bob_id}", json={"is_active": False})
    assert script.get("/api/v1/devices").status_code == 401

    assert admin_client.post("/api/v1/api-keys", json={"label": "x", "days": 4000}).status_code == 422
    assert admin_client.post("/api/v1/api-keys", json={"label": "x", "scope": "root"}).status_code == 422
