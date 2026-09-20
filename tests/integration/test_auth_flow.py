def test_setup_required_when_no_users_exist(client):
    r = client.get("/api/v1/auth/setup/status")
    assert r.status_code == 200
    assert r.json() == {"setup_required": True}


def test_setup_creates_first_admin(client):
    r = client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    assert r.status_code == 201
    body = r.json()
    assert body["username"] == "admin"
    assert body["is_admin"] is True


def test_setup_cannot_run_twice(client):
    """Given a first administrator already exists, when setup is called
    again, then it is rejected (CLAUDE.md section 13)."""
    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    r = client.post("/api/v1/auth/setup", json={"username": "second", "password": "adminpass123"})
    assert r.status_code == 403


def test_login_with_wrong_password_fails(client):
    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_login_sets_session_and_csrf_cookies(client):
    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"})
    assert r.status_code == 200
    assert client.cookies.get("rd_session")
    assert client.cookies.get("rd_csrf")


def test_me_requires_authentication(client):
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401


def test_logout_revokes_session(admin_client):
    r = admin_client.post("/api/v1/auth/logout")
    assert r.status_code == 200
    r = admin_client.get("/api/v1/auth/me")
    assert r.status_code == 401


def test_state_changing_request_without_csrf_token_is_rejected(client):
    """Given a cookie-authenticated session, when a state-changing request
    is made without the CSRF header, then it is rejected (CLAUDE.md
    section 25)."""
    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    client.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"})
    r = client.post("/api/v1/users", json={"username": "bob", "password": "bobpassword1"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CSRF_FAILED"


def test_rustdesk_client_login_returns_expected_shape(client):
    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    r = client.post("/api/login", json={"username": "admin", "password": "adminpass123"})
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "access_token"
    assert "access_token" in body
    assert body["user"]["name"] == "admin"


def test_rustdesk_client_login_failure_returns_error_field(client):
    r = client.post("/api/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 200
    assert "error" in r.json()


def test_rustdesk_bearer_token_is_not_csrf_checked(admin_client):
    """A request authenticated via an explicit Authorization header (not an
    ambient cookie) is not subject to CSRF checks."""
    login = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"})
    token = login.json()["access_token"]
    fresh_headers = {"Authorization": f"Bearer {token}"}
    r = admin_client.post(
        "/api/v1/users",
        json={"username": "carol", "password": "carolpassword1"},
        headers={**fresh_headers, "X-CSRF-Token": ""},
    )
    assert r.status_code == 201
