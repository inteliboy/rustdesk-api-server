"""Who may read the client-reported connection / file-transfer logs
(/api/v1/connection-logs, /api/v1/file-logs). Same visibility model as
devices: admin sees all, others only what they own or is shared with them
(CLAUDE.md sections 12, 65 - IDOR)."""


def _make_user_token(admin_client, username):
    admin_client.post(
        "/api/v1/users", json={"username": username, "password": "password-" + username, "is_admin": False}
    )
    token = admin_client.post(
        "/api/login", json={"username": username, "password": "password-" + username}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _owned_device_with_logs(admin_client, headers, rustdesk_id, uuid):
    admin_client.post(
        "/api/sysinfo",
        json={"id": rustdesk_id, "uuid": uuid, "hostname": rustdesk_id + "-host", "os": "windows"},
        headers=headers,
    )
    admin_client.post(
        "/api/audit/conn",
        json={"id": rustdesk_id, "uuid": uuid, "conn_id": 1, "action": "new", "ip": "203.0.113.9"},
    )
    admin_client.post(
        "/api/audit/file", json={"id": rustdesk_id, "uuid": uuid, "type": 1, "path": "p", "info": "{}"}
    )
    admin_client.post(
        "/api/audit/alarm",
        json={"id": rustdesk_id, "uuid": uuid, "typ": 0, "conn_id": 1, "info": '{"ip":"198.51.100.4"}'},
    )


def test_requires_authentication(client):
    assert client.get("/api/v1/connection-logs").status_code == 401
    assert client.get("/api/v1/file-logs").status_code == 401
    assert client.get("/api/v1/alarm-logs").status_code == 401


def test_owner_sees_own_logs_and_others_do_not(admin_client):
    alice = _make_user_token(admin_client, "alice")
    bob = _make_user_token(admin_client, "bob")
    _owned_device_with_logs(admin_client, alice, "alice-dev", "ua")

    for path in ("/api/v1/connection-logs", "/api/v1/file-logs", "/api/v1/alarm-logs"):
        assert admin_client.get(path, headers=alice).json()["total"] == 1
        assert admin_client.get(path, headers=bob).json()["total"] == 0
        assert admin_client.get(path).json()["total"] == 1  # admin (cookie session)


def test_unowned_device_logs_are_admin_only(admin_client):
    alice = _make_user_token(admin_client, "alice")
    _owned_device_with_logs(admin_client, {}, "orphan", "uo")

    assert admin_client.get("/api/v1/connection-logs", headers=alice).json()["total"] == 0
    assert admin_client.get("/api/v1/connection-logs").json()["total"] == 1


def test_shared_user_sees_logs_until_the_share_is_revoked(admin_client):
    alice = _make_user_token(admin_client, "alice")
    bob = _make_user_token(admin_client, "bob")
    _owned_device_with_logs(admin_client, alice, "alice-dev", "ua")
    device_id = admin_client.get("/api/v1/devices", headers=alice).json()["items"][0]["id"]

    share = admin_client.post(
        f"/api/v1/devices/{device_id}/shares", json={"username": "bob", "permission": "view"}, headers=alice
    ).json()
    assert admin_client.get("/api/v1/connection-logs", headers=bob).json()["total"] == 1

    admin_client.delete(f"/api/v1/devices/{device_id}/shares/{share['id']}", headers=alice)
    assert admin_client.get("/api/v1/connection-logs", headers=bob).json()["total"] == 0


def test_device_filter_on_invisible_device_is_404_not_empty(admin_client):
    """A 404 (same as a nonexistent id) so the endpoint cannot confirm
    another user's device exists."""
    alice = _make_user_token(admin_client, "alice")
    bob = _make_user_token(admin_client, "bob")
    _owned_device_with_logs(admin_client, alice, "alice-dev", "ua")
    device_id = admin_client.get("/api/v1/devices", headers=alice).json()["items"][0]["id"]

    assert admin_client.get(f"/api/v1/connection-logs?device_id={device_id}", headers=bob).status_code == 404
    assert admin_client.get("/api/v1/file-logs?device_id=999999", headers=bob).status_code == 404
    assert admin_client.get(f"/api/v1/alarm-logs?device_id={device_id}", headers=bob).status_code == 404
    assert (
        admin_client.get(f"/api/v1/connection-logs?device_id={device_id}", headers=alice).status_code == 200
    )


def test_rows_include_device_label_and_are_newest_first(admin_client):
    alice = _make_user_token(admin_client, "alice")
    _owned_device_with_logs(admin_client, alice, "alice-dev", "ua")
    admin_client.post(
        "/api/audit/conn", json={"id": "alice-dev", "uuid": "ua", "conn_id": 2, "action": "new"}
    )
    body = admin_client.get("/api/v1/connection-logs", headers=alice).json()
    assert [r["device_label"] for r in body["items"]] == ["alice-dev-host", "alice-dev-host"]
    assert body["items"][0]["id"] > body["items"][1]["id"]


def test_pagination(admin_client):
    _owned_device_with_logs(admin_client, {}, "dev", "u")
    for i in range(2, 6):
        admin_client.post("/api/audit/conn", json={"id": "dev", "uuid": "u", "conn_id": i, "action": "new"})
    body = admin_client.get("/api/v1/connection-logs?page=2&page_size=2").json()
    assert body["total"] == 5 and body["page"] == 2 and len(body["items"]) == 2


def test_logs_survive_device_deletion_for_admin_only(admin_client):
    alice = _make_user_token(admin_client, "alice")
    _owned_device_with_logs(admin_client, alice, "alice-dev", "ua")
    device_id = admin_client.get("/api/v1/devices", headers=alice).json()["items"][0]["id"]
    assert admin_client.delete(f"/api/v1/devices/{device_id}").status_code == 204

    admin_rows = admin_client.get("/api/v1/connection-logs").json()
    assert admin_rows["total"] == 1 and admin_rows["items"][0]["device_id"] is None
    assert admin_client.get("/api/v1/connection-logs", headers=alice).json()["total"] == 0
