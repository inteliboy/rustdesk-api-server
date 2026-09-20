def _register_device(client, rustdesk_id, token, hostname="host"):
    r = client.post(
        "/api/sysinfo",
        json={"id": rustdesk_id, "hostname": hostname, "os": "windows", "version": "1.0.0"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def _create_user(client, username, password):
    client.post("/api/v1/users", json={"username": username, "password": password, "is_admin": False})


def _login_webui(client, username, password):
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login", json={"username": username, "password": password})
    csrf = client.cookies.get("rd_csrf")
    client.headers.update({"X-CSRF-Token": csrf})


def test_owner_can_share_device_with_view_permission(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    _create_user(admin_client, "bob", "bobpassword1")
    r = admin_client.post(
        f"/api/v1/devices/{device_id}/shares", json={"username": "bob", "permission": "view"}
    )
    assert r.status_code == 201
    assert r.json()["shared_with_username"] == "bob"


def test_shared_user_can_view_but_not_edit_or_delete(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    _create_user(admin_client, "bob", "bobpassword1")
    admin_client.post(f"/api/v1/devices/{device_id}/shares", json={"username": "bob", "permission": "view"})

    _login_webui(admin_client, "bob", "bobpassword1")

    r = admin_client.get(f"/api/v1/devices/{device_id}")
    assert r.status_code == 200

    r = admin_client.get("/api/v1/devices")
    assert any(d["id"] == device_id for d in r.json()["items"])

    r = admin_client.patch(f"/api/v1/devices/{device_id}", json={"alias": "hijacked"})
    assert r.status_code == 404

    r = admin_client.delete(f"/api/v1/devices/{device_id}")
    assert r.status_code == 404


def test_control_share_lets_user_edit_alias(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    _create_user(admin_client, "bob", "bobpassword1")
    admin_client.post(
        f"/api/v1/devices/{device_id}/shares", json={"username": "bob", "permission": "control"}
    )

    _login_webui(admin_client, "bob", "bobpassword1")

    r = admin_client.patch(f"/api/v1/devices/{device_id}", json={"alias": "renamed"})
    assert r.status_code == 200
    assert r.json()["alias"] == "renamed"

    r = admin_client.delete(f"/api/v1/devices/{device_id}")
    assert r.status_code == 404


def test_shared_user_cannot_manage_shares_or_reshare(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    _create_user(admin_client, "bob", "bobpassword1")
    _create_user(admin_client, "carol", "carolpassword1")
    admin_client.post(
        f"/api/v1/devices/{device_id}/shares", json={"username": "bob", "permission": "control"}
    )

    _login_webui(admin_client, "bob", "bobpassword1")

    r = admin_client.get(f"/api/v1/devices/{device_id}/shares")
    assert r.status_code == 404
    r = admin_client.post(
        f"/api/v1/devices/{device_id}/shares", json={"username": "carol", "permission": "view"}
    )
    assert r.status_code == 404


def test_owner_can_revoke_a_share(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    _create_user(admin_client, "bob", "bobpassword1")
    share = admin_client.post(
        f"/api/v1/devices/{device_id}/shares", json={"username": "bob", "permission": "view"}
    ).json()

    r = admin_client.delete(f"/api/v1/devices/{device_id}/shares/{share['id']}")
    assert r.status_code == 204

    _login_webui(admin_client, "bob", "bobpassword1")
    r = admin_client.get(f"/api/v1/devices/{device_id}")
    assert r.status_code == 404


def test_cannot_share_with_nonexistent_user(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    r = admin_client.post(
        f"/api/v1/devices/{device_id}/shares", json={"username": "ghost", "permission": "view"}
    )
    assert r.status_code == 404


def test_cannot_share_device_twice_with_same_user(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    _create_user(admin_client, "bob", "bobpassword1")
    admin_client.post(f"/api/v1/devices/{device_id}/shares", json={"username": "bob", "permission": "view"})
    r = admin_client.post(
        f"/api/v1/devices/{device_id}/shares", json={"username": "bob", "permission": "control"}
    )
    assert r.status_code == 409


def test_invalid_permission_rejected(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    _create_user(admin_client, "bob", "bobpassword1")
    r = admin_client.post(
        f"/api/v1/devices/{device_id}/shares", json={"username": "bob", "permission": "root"}
    )
    assert r.status_code == 400


def test_non_owner_cannot_see_shares_for_a_device_they_do_not_own(admin_client):
    """IDOR check: GET /shares for a device you can't manage must 404, not
    leak who else it's shared with (CLAUDE.md section 65)."""
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    _create_user(admin_client, "bob", "bobpassword1")
    _create_user(admin_client, "carol", "carolpassword1")

    _login_webui(admin_client, "carol", "carolpassword1")
    r = admin_client.get(f"/api/v1/devices/{device_id}/shares")
    assert r.status_code == 404
