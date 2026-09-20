"""RustDesk client currentUser/logout protocol.

NOT YET VERIFIED against a real RustDesk desktop client - see
docs/rustdesk-compatibility.md.
"""


def _login(client) -> str:
    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    r = client.post("/api/login", json={"username": "admin", "password": "adminpass123"})
    return r.json()["access_token"]


def test_current_user_with_valid_token_returns_name(client):
    token = _login(client)
    r = client.post("/api/currentUser", json={}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "admin"
    assert body["type"] == "access_token"


def test_current_user_without_token_returns_error(client):
    r = client.post("/api/currentUser", json={})
    assert r.status_code == 200
    assert "error" in r.json()


def test_current_user_with_garbage_token_returns_error(client):
    r = client.post("/api/currentUser", json={}, headers={"Authorization": "Bearer not-a-real-token"})
    assert "error" in r.json()


def test_logout_revokes_token_for_future_requests(client):
    token = _login(client)
    r = client.post("/api/logout", json={}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == {"code": 1}

    r = client.post("/api/currentUser", json={}, headers={"Authorization": f"Bearer {token}"})
    assert "error" in r.json()


def test_logout_without_token_returns_error_not_crash(client):
    r = client.post("/api/logout", json={})
    assert r.status_code == 200
    assert "error" in r.json()
