"""RustDesk client login protocol (POST /api/login).

NOT YET VERIFIED against a real RustDesk desktop client - see
docs/rustdesk-compatibility.md. These tests pin down the behavior this
server currently implements so regressions are caught even before that
verification happens.
"""


def test_login_request_accepts_full_client_payload_shape(client):
    """The real client sends more fields than we strictly need (autoLogin,
    type, deviceInfo, uuid) - the endpoint must not reject unknown-but-
    documented fields."""
    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    r = client.post(
        "/api/login",
        json={
            "username": "admin",
            "password": "adminpass123",
            "id": "123456789",
            "uuid": "some-uuid",
            "autoLogin": True,
            "type": "",
            "deviceInfo": {"os": "windows", "type": "client"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"access_token", "type", "user"}
    assert body["type"] == "access_token"


def test_login_missing_credentials_returns_error_field_not_500(client):
    r = client.post("/api/login", json={})
    assert r.status_code == 200
    assert "error" in r.json()


def test_login_wrong_password_returns_error_field(client):
    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 200
    assert "error" in r.json()
    assert "access_token" not in r.json()


def test_login_with_device_id_registers_the_device(client):
    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    r = client.post("/api/login", json={"username": "admin", "password": "adminpass123", "id": "dev-xyz"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    devices = client.get("/api/v1/devices", headers={"Authorization": f"Bearer {token}"}).json()
    assert any(d["rustdesk_id"] == "dev-xyz" for d in devices["items"])
