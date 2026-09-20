def test_non_admin_cannot_list_users(admin_client):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    admin_client.post("/api/v1/auth/logout")
    admin_client.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepassword1"})
    csrf = admin_client.cookies.get("rd_csrf")
    admin_client.headers.update({"X-CSRF-Token": csrf})

    r = admin_client.get("/api/v1/users")
    assert r.status_code == 403


def test_admin_can_create_and_list_users(admin_client):
    r = admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    assert r.status_code == 201

    r = admin_client.get("/api/v1/users")
    usernames = {u["username"] for u in r.json()}
    assert usernames == {"admin", "alice"}


def test_creating_duplicate_username_fails(admin_client):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    r = admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "somethingelse1", "is_admin": False}
    )
    assert r.status_code == 409


def test_admin_cannot_deactivate_self(admin_client):
    me = admin_client.get("/api/v1/auth/me").json()
    r = admin_client.patch(f"/api/v1/users/{me['id']}", json={"is_active": False})
    assert r.status_code == 400


def test_admin_cannot_delete_self(admin_client):
    me = admin_client.get("/api/v1/auth/me").json()
    r = admin_client.delete(f"/api/v1/users/{me['id']}")
    assert r.status_code == 400


def test_deactivating_user_revokes_their_sessions(admin_client):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    users = admin_client.get("/api/v1/users").json()
    alice_id = next(u["id"] for u in users if u["username"] == "alice")

    r = admin_client.post("/api/login", json={"username": "alice", "password": "alicepassword1"})
    alice_token = r.json()["access_token"]

    admin_client.patch(f"/api/v1/users/{alice_id}", json={"is_active": False})

    r = admin_client.post("/api/currentUser", json={}, headers={"Authorization": f"Bearer {alice_token}"})
    assert "error" in r.json()


def test_reset_password_lets_user_log_in_with_new_password(admin_client):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    users = admin_client.get("/api/v1/users").json()
    alice_id = next(u["id"] for u in users if u["username"] == "alice")

    r = admin_client.post(f"/api/v1/users/{alice_id}/reset-password", json={"password": "newpassword1"})
    assert r.status_code == 200

    r = admin_client.post("/api/login", json={"username": "alice", "password": "newpassword1"})
    assert "access_token" in r.json()

    r = admin_client.post("/api/login", json={"username": "alice", "password": "alicepassword1"})
    assert "error" in r.json()


def test_user_can_view_own_profile_but_not_others(admin_client):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    users = admin_client.get("/api/v1/users").json()
    admin_id = next(u["id"] for u in users if u["username"] == "admin")
    alice_id = next(u["id"] for u in users if u["username"] == "alice")

    admin_client.post("/api/v1/auth/logout")
    admin_client.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepassword1"})
    csrf = admin_client.cookies.get("rd_csrf")
    admin_client.headers.update({"X-CSRF-Token": csrf})

    r = admin_client.get(f"/api/v1/users/{alice_id}")
    assert r.status_code == 200

    r = admin_client.get(f"/api/v1/users/{admin_id}")
    assert r.status_code == 404
