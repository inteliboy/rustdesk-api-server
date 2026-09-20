def _register_device(client, rustdesk_id, token, hostname="host"):
    r = client.post(
        "/api/sysinfo",
        json={"id": rustdesk_id, "hostname": hostname, "os": "windows", "version": "1.0.0"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def test_any_authenticated_user_can_create_a_tag(admin_client):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    admin_client.post("/api/v1/auth/logout")
    admin_client.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepassword1"})
    csrf = admin_client.cookies.get("rd_csrf")
    admin_client.headers.update({"X-CSRF-Token": csrf})

    r = admin_client.post("/api/v1/tags", json={"name": "prod", "color": "#ff0000"})
    assert r.status_code == 201


def test_non_admin_cannot_rename_or_delete_a_tag(admin_client):
    tag = admin_client.post("/api/v1/tags", json={"name": "prod"}).json()

    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    admin_client.post("/api/v1/auth/logout")
    admin_client.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepassword1"})
    csrf = admin_client.cookies.get("rd_csrf")
    admin_client.headers.update({"X-CSRF-Token": csrf})

    r = admin_client.patch(f"/api/v1/tags/{tag['id']}", json={"name": "renamed"})
    assert r.status_code == 403
    r = admin_client.delete(f"/api/v1/tags/{tag['id']}")
    assert r.status_code == 403


def test_duplicate_tag_name_rejected(admin_client):
    admin_client.post("/api/v1/tags", json={"name": "prod"})
    r = admin_client.post("/api/v1/tags", json={"name": "prod"})
    assert r.status_code == 409


def test_attach_and_filter_devices_by_tag(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token, hostname="a")
    _register_device(admin_client, "dev-2", token, hostname="b")
    items = admin_client.get("/api/v1/devices").json()["items"]
    dev1_id = next(d["id"] for d in items if d["rustdesk_id"] == "dev-1")

    tag = admin_client.post("/api/v1/tags", json={"name": "prod"}).json()
    r = admin_client.patch(f"/api/v1/devices/{dev1_id}", json={"tag_ids": [tag["id"]]})
    assert r.status_code == 200
    assert r.json()["tags"] == [{"id": tag["id"], "name": "prod", "color": "#64748b"}]

    r = admin_client.get("/api/v1/devices", params={"tag_id": tag["id"]})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["rustdesk_id"] == "dev-1"


def test_replacing_tag_ids_removes_old_tags(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    _register_device(admin_client, "dev-1", token)
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]

    tag_a = admin_client.post("/api/v1/tags", json={"name": "a"}).json()
    tag_b = admin_client.post("/api/v1/tags", json={"name": "b"}).json()

    admin_client.patch(f"/api/v1/devices/{device_id}", json={"tag_ids": [tag_a["id"], tag_b["id"]]})
    r = admin_client.patch(f"/api/v1/devices/{device_id}", json={"tag_ids": [tag_b["id"]]})
    tag_names = {t["name"] for t in r.json()["tags"]}
    assert tag_names == {"b"}
