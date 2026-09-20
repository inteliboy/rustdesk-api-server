"""RustDesk client audit endpoints (POST /api/audit/conn and /api/audit/file).

Paths and payload shapes are taken from the real client source (see
docs/rustdesk-compatibility.md) but NOT yet verified against live traffic
from a real client - that needs a second machine connecting in.
"""

import json

from rustdesk_api.security.rate_limit import RateLimiter


def _register(client, rustdesk_id="dev-a", uuid="uuid-a"):
    client.post("/api/sysinfo", json={"id": rustdesk_id, "uuid": uuid, "hostname": "h", "os": "windows"})


def _conn(client, **body):
    return client.post("/api/audit/conn", json={"id": "dev-a", "uuid": "uuid-a", **body})


def _conn_logs(admin_client):
    return admin_client.get("/api/v1/connection-logs").json()


def test_connection_lifecycle_new_authorize_close(admin_client):
    _register(admin_client)

    r = _conn(admin_client, conn_id=5, session_id=123456789, nonce="n1", action="new", ip="203.0.113.7")
    assert r.status_code == 200 and r.content == b""
    r = _conn(
        admin_client,
        conn_id=5,
        session_id=123456789,
        nonce="n2",
        peer=["111222333", "Bob"],
        type=0,
        primary_auth=2,
        two_factor=1,
    )
    assert r.status_code == 200
    r = _conn(admin_client, conn_id=5, session_id=123456789, nonce="n3", action="close")
    assert r.status_code == 200

    body = _conn_logs(admin_client)
    assert body["total"] == 1
    row = body["items"][0]
    assert row["rustdesk_id"] == "dev-a"
    assert row["from_ip"] == "203.0.113.7"
    assert row["peer_id"] == "111222333"
    assert row["peer_name"] == "Bob"
    assert row["conn_type"] == 0
    assert row["primary_auth"] == 2
    assert row["two_factor"] == 1
    assert row["ended_at"] is not None


def test_new_event_is_idempotent_by_nonce(admin_client):
    """A client retry after a timeout/5xx re-sends the same body; it must
    not create a second session row (CLAUDE.md section 43)."""
    _register(admin_client)
    for _ in range(3):
        _conn(admin_client, conn_id=1, nonce="same-nonce", action="new", ip="203.0.113.7")
    assert _conn_logs(admin_client)["total"] == 1


def test_close_without_an_open_session_is_ignored(admin_client):
    _register(admin_client)
    r = _conn(admin_client, conn_id=99, action="close")
    assert r.status_code == 200
    assert _conn_logs(admin_client)["total"] == 0


def test_authorize_update_without_prior_new_still_records(admin_client):
    _register(admin_client)
    _conn(admin_client, conn_id=7, peer=["42", "Eve"], type=1, primary_auth=1)
    row = _conn_logs(admin_client)["items"][0]
    assert row["peer_id"] == "42" and row["conn_type"] == 1


def test_unknown_device_is_dropped_but_answers_ok(admin_client):
    """Answering identically for unknown ids means the unauthenticated
    endpoint cannot be used to probe which device ids exist."""
    r = admin_client.post("/api/audit/conn", json={"id": "nobody", "conn_id": 1, "action": "new"})
    assert r.status_code == 200 and r.content == b""
    assert _conn_logs(admin_client)["total"] == 0


def test_uuid_mismatch_is_dropped(admin_client):
    _register(admin_client, uuid="real-uuid")
    r = admin_client.post(
        "/api/audit/conn", json={"id": "dev-a", "uuid": "forged", "conn_id": 1, "action": "new"}
    )
    assert r.status_code == 200
    assert _conn_logs(admin_client)["total"] == 0


def test_missing_uuid_is_dropped_when_device_has_one(admin_client):
    _register(admin_client, uuid="real-uuid")
    admin_client.post("/api/audit/conn", json={"id": "dev-a", "conn_id": 1, "action": "new"})
    assert _conn_logs(admin_client)["total"] == 0


def test_missing_id_returns_error(client):
    r = client.post("/api/audit/conn", json={"action": "new"})
    assert r.status_code == 200 and "error" in r.json()
    r = client.post("/api/audit/file", json={"path": "x"})
    assert r.status_code == 200 and "error" in r.json()


def test_file_audit_parses_info_json_string(admin_client):
    _register(admin_client)
    info = json.dumps({"ip": "203.0.113.7", "name": "Bob", "num": 2, "files": [["a.txt", 10], ["b.txt", 20]]})
    r = admin_client.post(
        "/api/audit/file",
        json={
            "id": "dev-a",
            "uuid": "uuid-a",
            "peer_id": "111",
            "conn_id": 5,
            "type": 1,
            "path": "C:\\Users\\bob",
            "is_file": False,
            "info": info,
            "nonce": "f1",
        },
    )
    assert r.status_code == 200 and r.content == b""

    body = admin_client.get("/api/v1/file-logs").json()
    row = body["items"][0]
    assert row["peer_id"] == "111"
    assert row["peer_name"] == "Bob"
    assert row["from_ip"] == "203.0.113.7"
    assert row["audit_type"] == 1
    assert row["path"] == "C:\\Users\\bob"
    assert row["num"] == 2
    assert row["files"] == [["a.txt", 10], ["b.txt", 20]]


def test_file_audit_is_idempotent_by_nonce(admin_client):
    _register(admin_client)
    payload = {"id": "dev-a", "uuid": "uuid-a", "type": 0, "nonce": "dup", "info": "{}"}
    for _ in range(3):
        admin_client.post("/api/audit/file", json=payload)
    assert admin_client.get("/api/v1/file-logs").json()["total"] == 1


def test_file_list_and_strings_are_capped(admin_client):
    _register(admin_client)
    files = [[f"file-{i}-" + "x" * 1000, i] for i in range(500)]
    admin_client.post(
        "/api/audit/file",
        json={
            "id": "dev-a",
            "uuid": "uuid-a",
            "type": 1,
            "nonce": "big",
            "path": "p" * 5000,
            "info": json.dumps({"num": 500, "files": files, "name": "n" * 1000}),
        },
    )
    row = admin_client.get("/api/v1/file-logs").json()["items"][0]
    assert len(row["files"]) == 200
    assert all(len(name) <= 255 for name, _ in row["files"])
    assert len(row["path"]) == 1024
    assert len(row["peer_name"]) == 255


def test_malformed_info_is_tolerated(admin_client):
    _register(admin_client)
    r = admin_client.post(
        "/api/audit/file", json={"id": "dev-a", "uuid": "uuid-a", "type": 1, "info": "not json {"}
    )
    assert r.status_code == 200
    assert admin_client.get("/api/v1/file-logs").json()["total"] == 1


def test_audit_endpoints_are_rate_limited_per_ip(client):
    client.app.state.client_audit_rate_limiter = RateLimiter(max_attempts=2, window_seconds=60)
    codes = [client.post("/api/audit/conn", json={"id": "x"}).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]
