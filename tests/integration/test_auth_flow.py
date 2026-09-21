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


def test_login_by_email_works_in_the_webui_and_the_rustdesk_client(admin_client):
    """Given an account with an e-mail address, when the client or the WebUI
    signs in with that address (in any letter case), then it succeeds."""
    for address in ("admin@example.com", "  Admin@Example.COM "):
        r = admin_client.post("/api/login", json={"username": address, "password": "adminpass123"})
        assert r.json()["user"]["name"] == "admin"
        r = admin_client.post("/api/v1/auth/login", json={"username": address, "password": "adminpass123"})
        assert r.status_code == 200
        assert r.json()["user"]["username"] == "admin"


def test_login_by_email_still_needs_the_password(admin_client):
    r = admin_client.post("/api/login", json={"username": "admin@example.com", "password": "wrong"})
    assert "error" in r.json()
    assert "access_token" not in r.json()


def test_a_username_wins_over_another_accounts_email(admin_client):
    """Registering someone else's sign-in name as an address must not let the
    holder of the address take the name over."""
    admin_client.post(
        "/api/v1/users",
        json={"username": "mallory", "password": "mallorypass1", "email": "boss@example.com"},
    )
    admin_client.post("/api/v1/users", json={"username": "boss@example.com", "password": "bosspassword1"})
    r = admin_client.post("/api/login", json={"username": "boss@example.com", "password": "bosspassword1"})
    assert r.json()["user"]["name"] == "boss@example.com"
    r = admin_client.post("/api/login", json={"username": "boss@example.com", "password": "mallorypass1"})
    assert "error" in r.json()


def test_a_name_without_an_at_sign_is_never_looked_up_as_an_email(admin_client):
    r = admin_client.post("/api/login", json={"username": "admin@example", "password": "adminpass123"})
    assert "error" in r.json()
