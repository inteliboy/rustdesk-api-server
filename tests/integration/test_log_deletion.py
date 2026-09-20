"""Deleting connection / file-transfer logs is administrator-only.

Given the logs are the record of who connected to a machine
When a non-admin (even the device's owner) tries to delete them
Then it is refused - and admin deletions leave an audit trail of their own.
"""

import pytest


def _make_user_headers(client, username):
    client.post(
        "/api/v1/users", json={"username": username, "password": "password-" + username, "is_admin": False}
    )
    token = client.post("/api/login", json={"username": username, "password": "password-" + username}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def _device_with_logs(client, headers, rustdesk_id, uuid):
    client.post(
        "/api/sysinfo",
        json={"id": rustdesk_id, "uuid": uuid, "hostname": rustdesk_id + "-host", "os": "windows"},
        headers=headers,
    )
    client.post(
        "/api/audit/conn",
        json={"id": rustdesk_id, "uuid": uuid, "conn_id": 1, "action": "new", "ip": "1.2.3.4"},
    )
    client.post(
        "/api/audit/file",
        json={"id": rustdesk_id, "uuid": uuid, "type": 1, "path": "C:/secret.txt", "info": "{}"},
    )
    client.post(
        "/api/audit/alarm",
        json={"id": rustdesk_id, "uuid": uuid, "typ": 0, "conn_id": 1, "info": '{"ip":"198.51.100.4"}'},
    )


def _total(client, kind):
    return client.get(f"/api/v1/{kind}").json()["total"]


def _audit_actions(client):
    return [i["action"] for i in client.get("/api/v1/admin/audit-logs").json()["items"]]


@pytest.mark.parametrize("kind", ["connection-logs", "file-logs", "alarm-logs"])
def test_admin_can_delete_a_single_log(admin_client, kind):
    _device_with_logs(admin_client, {}, "dev-1", "u1")
    log_id = admin_client.get(f"/api/v1/{kind}").json()["items"][0]["id"]

    assert admin_client.delete(f"/api/v1/{kind}/{log_id}").status_code == 204

    assert _total(admin_client, kind) == 0
    assert admin_client.delete(f"/api/v1/{kind}/{log_id}").status_code == 404  # already gone


@pytest.mark.parametrize("kind", ["connection-logs", "file-logs", "alarm-logs"])
def test_admin_can_clear_all_logs(admin_client, kind):
    _device_with_logs(admin_client, {}, "dev-1", "u1")
    _device_with_logs(admin_client, {}, "dev-2", "u2")
    assert _total(admin_client, kind) == 2

    r = admin_client.delete(f"/api/v1/{kind}")
    assert r.status_code == 200
    assert r.json() == {"deleted": 2}
    assert _total(admin_client, kind) == 0
    # Clearing an already-empty log is fine.
    assert admin_client.delete(f"/api/v1/{kind}").json() == {"deleted": 0}


@pytest.mark.parametrize("kind", ["connection-logs", "file-logs", "alarm-logs"])
def test_clearing_can_be_limited_to_one_device(admin_client, kind):
    _device_with_logs(admin_client, {}, "dev-1", "u1")
    _device_with_logs(admin_client, {}, "dev-2", "u2")
    first = next(
        i for i in admin_client.get("/api/v1/devices").json()["items"] if i["rustdesk_id"] == "dev-1"
    )

    assert admin_client.delete(f"/api/v1/{kind}", params={"device_id": first["id"]}).json() == {"deleted": 1}
    remaining = admin_client.get(f"/api/v1/{kind}").json()["items"]
    assert [r["rustdesk_id"] for r in remaining] == ["dev-2"]


@pytest.mark.parametrize("kind", ["connection-logs", "file-logs", "alarm-logs"])
def test_device_owner_cannot_delete_their_own_devices_logs(admin_client, kind):
    """The owner may read the trail for their machine but not erase it."""
    alice = _make_user_headers(admin_client, "alice")
    _device_with_logs(admin_client, alice, "alice-dev", "ua")
    log_id = admin_client.get(f"/api/v1/{kind}", headers=alice).json()["items"][0]["id"]

    assert admin_client.delete(f"/api/v1/{kind}/{log_id}", headers=alice).status_code == 403
    assert admin_client.delete(f"/api/v1/{kind}", headers=alice).status_code == 403
    assert admin_client.get(f"/api/v1/{kind}", headers=alice).json()["total"] == 1


@pytest.mark.parametrize("kind", ["connection-logs", "file-logs", "alarm-logs"])
def test_deleting_requires_authentication(client, kind):
    assert client.delete(f"/api/v1/{kind}/1").status_code == 401
    assert client.delete(f"/api/v1/{kind}").status_code == 401


@pytest.mark.parametrize("kind", ["connection-logs", "file-logs", "alarm-logs"])
def test_cookie_session_deletes_need_a_csrf_token(admin_client, kind):
    _device_with_logs(admin_client, {}, "dev-1", "u1")
    csrf = admin_client.headers.pop("X-CSRF-Token")

    assert admin_client.delete(f"/api/v1/{kind}").status_code == 403
    assert _total_via_bearer(admin_client, kind) == 1

    admin_client.headers["X-CSRF-Token"] = csrf
    assert admin_client.delete(f"/api/v1/{kind}").status_code == 200


def _total_via_bearer(client, kind):
    token = client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    return client.get(f"/api/v1/{kind}", headers={"Authorization": f"Bearer {token}"}).json()["total"]


def test_deletions_are_audited_without_log_contents(admin_client):
    _device_with_logs(admin_client, {}, "dev-1", "u1")
    conn_id = admin_client.get("/api/v1/connection-logs").json()["items"][0]["id"]
    admin_client.delete(f"/api/v1/connection-logs/{conn_id}")
    admin_client.delete("/api/v1/file-logs")

    entries = {i["action"]: i for i in admin_client.get("/api/v1/admin/audit-logs").json()["items"]}
    assert {"connection_log_deleted", "file_logs_cleared"} <= set(_audit_actions(admin_client))
    assert entries["file_logs_cleared"]["detail"] == {"deleted": 1, "device_id": None}
    assert entries["connection_log_deleted"]["detail"] == {"rustdesk_id": "dev-1"}
    assert entries["connection_log_deleted"]["target_id"] == conn_id
    assert "C:/secret.txt" not in str(entries)  # contents never copied into the audit trail


def test_alarm_log_deletions_are_audited_without_log_contents(admin_client):
    _device_with_logs(admin_client, {}, "dev-1", "u1")
    alarm_id = admin_client.get("/api/v1/alarm-logs").json()["items"][0]["id"]
    admin_client.delete(f"/api/v1/alarm-logs/{alarm_id}")
    admin_client.delete("/api/v1/alarm-logs")

    entries = {i["action"]: i for i in admin_client.get("/api/v1/admin/audit-logs").json()["items"]}
    assert {"alarm_log_deleted", "alarm_logs_cleared"} <= set(_audit_actions(admin_client))
    assert entries["alarm_log_deleted"]["detail"] == {"rustdesk_id": "dev-1"}
    assert entries["alarm_log_deleted"]["target_id"] == alarm_id
    assert entries["alarm_logs_cleared"]["detail"] == {"deleted": 0, "device_id": None}
    assert "198.51.100.4" not in str(entries)
