def _register_device(client, rustdesk_id, token, hostname="host", os="windows"):
    r = client.post(
        "/api/sysinfo",
        json={"id": rustdesk_id, "hostname": hostname, "os": os, "version": "1.0.0"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def _create_user_and_login_rustdesk(client, username, password):
    client.post(
        "/api/v1/users",
        json={"username": username, "password": password, "is_admin": False},
    )
    r = client.post("/api/login", json={"username": username, "password": password})
    return r.json()["access_token"]


def test_admin_sees_all_devices(admin_client):
    admin_token = admin_client.post(
        "/api/login", json={"username": "admin", "password": "adminpass123"}
    ).json()["access_token"]
    _register_device(admin_client, "dev-admin", admin_token)

    user_token = _create_user_and_login_rustdesk(admin_client, "alice", "alicepassword1")
    _register_device(admin_client, "dev-alice", user_token)

    r = admin_client.get("/api/v1/devices")
    assert r.status_code == 200
    ids = {d["rustdesk_id"] for d in r.json()["items"]}
    assert ids == {"dev-admin", "dev-alice"}


def test_user_cannot_access_another_users_device_by_guessing_id(admin_client):
    """Given a device owned by user A, when user B requests it directly by
    id (IDOR probe), then the server returns 404, not the device data
    (CLAUDE.md section 65)."""
    user_a_token = _create_user_and_login_rustdesk(admin_client, "alice", "alicepassword1")
    _register_device(admin_client, "dev-alice", user_a_token)

    devices = admin_client.get("/api/v1/devices").json()["items"]
    alice_device_id = next(d["id"] for d in devices if d["rustdesk_id"] == "dev-alice")

    admin_client.post(
        "/api/v1/users", json={"username": "bob", "password": "bobpassword1", "is_admin": False}
    )
    admin_client.post("/api/v1/auth/logout")

    bob_client = admin_client
    bob_client.post("/api/v1/auth/login", json={"username": "bob", "password": "bobpassword1"})
    csrf = bob_client.cookies.get("rd_csrf")
    bob_client.headers.update({"X-CSRF-Token": csrf})

    r = bob_client.get(f"/api/v1/devices/{alice_device_id}")
    assert r.status_code == 404

    r = bob_client.get("/api/v1/devices")
    assert r.json()["total"] == 0


def test_admin_can_filter_devices_by_owner_id(admin_client):
    admin_token = admin_client.post(
        "/api/login", json={"username": "admin", "password": "adminpass123"}
    ).json()["access_token"]
    _register_device(admin_client, "dev-admin", admin_token)

    user_token = _create_user_and_login_rustdesk(admin_client, "alice", "alicepassword1")
    _register_device(admin_client, "dev-alice", user_token)

    alice_id = next(u["id"] for u in admin_client.get("/api/v1/users").json() if u["username"] == "alice")
    r = admin_client.get(f"/api/v1/devices?owner_id={alice_id}")
    assert r.status_code == 200
    ids = {d["rustdesk_id"] for d in r.json()["items"]}
    assert ids == {"dev-alice"}


def test_non_admin_owner_id_filter_is_ignored(admin_client):
    """A non-admin passing someone else's owner_id must still only see
    their own devices - the server enforces this, not the query param
    (CLAUDE.md section 66)."""
    admin_token = admin_client.post(
        "/api/login", json={"username": "admin", "password": "adminpass123"}
    ).json()["access_token"]
    _register_device(admin_client, "dev-admin", admin_token)
    admin_id = admin_client.get("/api/v1/auth/me").json()["id"]

    admin_client.post(
        "/api/v1/users", json={"username": "bob", "password": "bobpassword1", "is_admin": False}
    )
    admin_client.post("/api/v1/auth/logout")
    bob_client = admin_client
    bob_client.post("/api/v1/auth/login", json={"username": "bob", "password": "bobpassword1"})
    csrf = bob_client.cookies.get("rd_csrf")
    bob_client.headers.update({"X-CSRF-Token": csrf})

    r = bob_client.get(f"/api/v1/devices?owner_id={admin_id}")
    assert r.status_code == 200
    assert r.json()["total"] == 0


def test_device_search_filters_results(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-one", token, hostname="server-alpha")
    _register_device(admin_client, "dev-two", token, hostname="server-beta")

    r = admin_client.get("/api/v1/devices", params={"search": "alpha"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["hostname"] == "server-alpha"


def test_device_list_is_paginated(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    for i in range(5):
        _register_device(admin_client, f"dev-{i}", token)

    r = admin_client.get("/api/v1/devices", params={"page": 1, "page_size": 2})
    body = r.json()
    assert len(body["items"]) == 2
    assert body["total"] == 5
    assert body["page"] == 1


def test_update_device_alias(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    r = admin_client.patch(f"/api/v1/devices/{device_id}", json={"alias": "My PC"})
    assert r.status_code == 200
    assert r.json()["alias"] == "My PC"


def test_delete_device(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    r = admin_client.delete(f"/api/v1/devices/{device_id}")
    assert r.status_code == 204
    r = admin_client.get(f"/api/v1/devices/{device_id}")
    assert r.status_code == 404


def test_only_admin_can_reassign_device_owner(admin_client):
    user_token = _create_user_and_login_rustdesk(admin_client, "alice", "alicepassword1")
    _register_device(admin_client, "dev-alice", user_token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    admin_client.post(
        "/api/v1/users", json={"username": "bob", "password": "bobpassword1", "is_admin": False}
    )
    admin_client.post("/api/v1/auth/logout")
    admin_client.post("/api/v1/auth/login", json={"username": "bob", "password": "bobpassword1"})
    csrf = admin_client.cookies.get("rd_csrf")
    admin_client.headers.update({"X-CSRF-Token": csrf})

    r = admin_client.patch(f"/api/v1/devices/{device_id}", json={"owner_id": 999})
    assert r.status_code == 404  # bob cannot even see alice's device
