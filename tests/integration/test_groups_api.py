def _register_device(client, rustdesk_id, token, hostname="host"):
    r = client.post(
        "/api/sysinfo",
        json={"id": rustdesk_id, "hostname": hostname, "os": "windows", "version": "1.0.0"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def test_create_and_list_groups(admin_client):
    r = admin_client.post("/api/v1/groups", json={"name": "Servers", "description": "Prod boxes"})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Servers"
    assert body["device_count"] == 0

    r = admin_client.get("/api/v1/groups")
    assert len(r.json()) == 1


def test_user_cannot_see_another_users_group(admin_client):
    admin_client.post("/api/v1/groups", json={"name": "Admin Group"})

    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    admin_client.post("/api/v1/auth/logout")
    admin_client.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepassword1"})
    csrf = admin_client.cookies.get("rd_csrf")
    admin_client.headers.update({"X-CSRF-Token": csrf})

    r = admin_client.get("/api/v1/groups")
    assert r.json() == []


def test_assigning_device_to_group_updates_device_count(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    group = admin_client.post("/api/v1/groups", json={"name": "Servers"}).json()

    r = admin_client.patch(f"/api/v1/devices/{device_id}", json={"group_id": group["id"]})
    assert r.status_code == 200
    assert r.json()["group_id"] == group["id"]
    assert r.json()["group_name"] == "Servers"

    r = admin_client.get("/api/v1/groups")
    assert r.json()[0]["device_count"] == 1


def test_cannot_assign_device_to_someone_elses_group(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)

    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    alice_token = admin_client.post(
        "/api/login", json={"username": "alice", "password": "alicepassword1"}
    ).json()["access_token"]
    _register_device(admin_client, "dev-alice", alice_token)

    group = admin_client.post("/api/v1/groups", json={"name": "AdminGroup"}).json()

    admin_client.post("/api/v1/auth/logout")
    admin_client.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepassword1"})
    csrf = admin_client.cookies.get("rd_csrf")
    admin_client.headers.update({"X-CSRF-Token": csrf})

    alice_device_id = next(
        d["id"]
        for d in admin_client.get("/api/v1/devices").json()["items"]
        if d["rustdesk_id"] == "dev-alice"
    )
    r = admin_client.patch(f"/api/v1/devices/{alice_device_id}", json={"group_id": group["id"]})
    assert r.status_code == 404


def test_deleting_group_unassigns_devices_without_deleting_them(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]
    group = admin_client.post("/api/v1/groups", json={"name": "Servers"}).json()
    admin_client.patch(f"/api/v1/devices/{device_id}", json={"group_id": group["id"]})

    r = admin_client.delete(f"/api/v1/groups/{group['id']}")
    assert r.status_code == 204

    r = admin_client.get(f"/api/v1/devices/{device_id}")
    assert r.status_code == 200
    assert r.json()["group_id"] is None


def test_filter_devices_by_group(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token, hostname="a")
    _register_device(admin_client, "dev-2", token, hostname="b")
    items = admin_client.get("/api/v1/devices").json()["items"]
    dev1_id = next(d["id"] for d in items if d["rustdesk_id"] == "dev-1")

    group = admin_client.post("/api/v1/groups", json={"name": "Servers"}).json()
    admin_client.patch(f"/api/v1/devices/{dev1_id}", json={"group_id": group["id"]})

    r = admin_client.get("/api/v1/devices", params={"group_id": group["id"]})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["rustdesk_id"] == "dev-1"
