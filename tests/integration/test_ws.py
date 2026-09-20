"""Live device-status WebSocket (/api/v1/ws/devices) - see rustdesk_api/ws.py
and api/ws.py. Uses Starlette's TestClient websocket support, which shares
cookies with the same client instance (CLAUDE.md section 30: exercise real
HTTP/WS behavior, not just internal functions)."""

import pytest
from starlette.websockets import WebSocketDisconnect


def test_ws_rejects_unauthenticated_connection(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/ws/devices"):
            pass


def test_ws_receives_device_update_on_heartbeat(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]

    with admin_client.websocket_connect("/api/v1/ws/devices") as ws:
        r = admin_client.post(
            "/api/sysinfo",
            json={"id": "ws-dev-1", "hostname": "host", "os": "windows"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200

        msg = ws.receive_json()
        assert msg["type"] == "device_updated"
        assert msg["device"]["rustdesk_id"] == "ws-dev-1"
        assert msg["device"]["online"] is True


def test_ws_does_not_broadcast_devices_the_connected_user_cannot_see(admin_client):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    alice_token = admin_client.post(
        "/api/login", json={"username": "alice", "password": "alicepassword1"}
    ).json()["access_token"]
    alice_auth = {"Authorization": f"Bearer {alice_token}"}

    admin_client.post("/api/v1/auth/logout")
    admin_client.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepassword1"})
    csrf = admin_client.cookies.get("rd_csrf")
    admin_client.headers.update({"X-CSRF-Token": csrf})

    with admin_client.websocket_connect("/api/v1/ws/devices") as ws:
        # A device owned by alice (the connected user) - should broadcast.
        r = admin_client.post(
            "/api/sysinfo", json={"id": "alice-dev", "hostname": "h", "os": "windows"}, headers=alice_auth
        )
        assert r.status_code == 200
        msg = ws.receive_json()
        assert msg["device"]["rustdesk_id"] == "alice-dev"

        # A device with no owner at all - alice cannot view it, so this
        # update must never reach her connection.
        r = admin_client.post("/api/sysinfo", json={"id": "unowned-dev", "hostname": "h", "os": "windows"})
        assert r.status_code == 200

        # Broadcasts are delivered in order, so if the unowned-dev update
        # had (incorrectly) been queued for this connection, it would
        # arrive before this next one - asserting this is the next message
        # received proves it never reached her.
        r = admin_client.post(
            "/api/sysinfo", json={"id": "alice-dev-2", "hostname": "h", "os": "windows"}, headers=alice_auth
        )
        assert r.status_code == 200
        msg = ws.receive_json()
        assert msg["device"]["rustdesk_id"] == "alice-dev-2"
