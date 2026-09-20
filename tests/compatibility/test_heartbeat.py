"""RustDesk client heartbeat protocol (POST /api/heartbeat).

NOT YET VERIFIED against a real RustDesk desktop client - see
docs/rustdesk-compatibility.md.
"""

import time


def test_heartbeat_for_unknown_device_does_not_error(client):
    """The real client may heartbeat before/without ever calling sysinfo in
    some flows; the server must not crash on that."""
    r = client.post("/api/heartbeat", json={"id": "never-registered"})
    assert r.status_code == 200
    assert r.json()["data"] == "OK"


def test_heartbeat_for_unknown_device_asks_the_client_to_send_its_sysinfo(client):
    """`/api/sysinfo` is acknowledged with SYSINFO_UPDATED, after which the client stops re-sending it.
    A device we no longer know (fresh database, deleted by an administrator) has to be able to get
    itself back, and the client's protocol for that is a `sysinfo` key in the heartbeat response."""
    r = client.post("/api/heartbeat", json={"id": "never-registered"})
    assert r.json() == {"data": "OK", "sysinfo": True}

    assert client.post("/api/sysinfo", json={"id": "never-registered", "hostname": "h"}).status_code == 200
    assert client.post("/api/heartbeat", json={"id": "never-registered"}).json() == {"data": "OK"}


def test_a_deleted_device_is_asked_for_its_sysinfo_again(admin_client):
    admin_client.post("/api/sysinfo", json={"id": "dev-gone", "hostname": "h"})
    (device,) = admin_client.get("/api/v1/devices").json()["items"]
    assert admin_client.post("/api/heartbeat", json={"id": "dev-gone"}).json() == {"data": "OK"}

    r = admin_client.delete(f"/api/v1/devices/{device['id']}")
    assert r.status_code == 204, r.text
    assert admin_client.post("/api/heartbeat", json={"id": "dev-gone"}).json()["sysinfo"] is True


def test_heartbeat_updates_last_seen_without_duplicating_device(client, admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    admin_client.post(
        "/api/sysinfo",
        json={"id": "dev-1", "hostname": "host-1", "os": "linux", "version": "1.0.0"},
        headers={"Authorization": f"Bearer {token}"},
    )
    first = admin_client.get("/api/v1/devices").json()["items"][0]
    first_seen = first["last_seen"]

    time.sleep(0.01)
    admin_client.post("/api/heartbeat", json={"id": "dev-1"})

    body = admin_client.get("/api/v1/devices").json()
    assert body["total"] == 1
    assert body["items"][0]["last_seen"] >= first_seen


def test_heartbeat_missing_id_returns_error(client):
    r = client.post("/api/heartbeat", json={})
    assert r.status_code == 200
    assert "error" in r.json()
