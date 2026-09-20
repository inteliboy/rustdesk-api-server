"""RustDesk client alarm endpoint (POST /api/audit/alarm).

Payload shapes come from the real client source (`post_alarm_audit` and its
callers in src/server/connection.rs, at both the 1.4.9 tag and master) but are
NOT yet verified against live traffic: provoking an alarm needs a second
machine that is refused (a whitelist, or repeated wrong passwords).
"""

import json

from rustdesk_api.security.rate_limit import RateLimiter


def _register(client, rustdesk_id="dev-a", uuid="uuid-a"):
    client.post("/api/sysinfo", json={"id": rustdesk_id, "uuid": uuid, "hostname": "h", "os": "windows"})


def _alarm(client, **body):
    return client.post("/api/audit/alarm", json={"id": "dev-a", "uuid": "uuid-a", **body})


def _logs(admin_client):
    return admin_client.get("/api/v1/alarm-logs").json()


def test_ip_whitelist_alarm_is_recorded_and_answered_with_an_empty_body(admin_client):
    """Newer clients read an empty 2xx body as "stored" and retry anything
    else, so the body must be empty (1.4.9 ignores it)."""
    _register(admin_client)
    r = _alarm(admin_client, typ=0, conn_id=3, info=json.dumps({"ip": "203.0.113.50"}))
    assert r.status_code == 200
    assert r.content == b""

    body = _logs(admin_client)
    assert body["total"] == 1
    row = body["items"][0]
    assert row["rustdesk_id"] == "dev-a"
    assert row["device_label"] == "h"
    assert row["alarm_type"] == 0
    assert row["from_ip"] == "203.0.113.50"
    assert row["peer_id"] is None and row["message"] is None


def test_failed_login_alarm_records_the_refused_peer(admin_client):
    _register(admin_client)
    info = json.dumps({"ip": "203.0.113.51", "id": "987654321", "name": "attacker-pc"})
    _alarm(admin_client, typ=2, conn_id=4, info=info, nonce="6b1f8c2e-0000-4000-8000-000000000001")

    row = _logs(admin_client)["items"][0]
    assert (row["alarm_type"], row["from_ip"], row["peer_id"], row["peer_name"]) == (
        2,
        "203.0.113.51",
        "987654321",
        "attacker-pc",
    )


def test_session_scope_violation_alarm_keeps_conn_type_and_message(admin_client):
    _register(admin_client)
    info = json.dumps(
        {"id": "555", "name": "Bob", "ip": "203.0.113.52", "conn_type": "file_transfer", "message": "denied"}
    )
    _alarm(admin_client, typ=9, conn_id=5, info=info)

    row = _logs(admin_client)["items"][0]
    assert row["alarm_type"] == 9
    assert row["conn_type"] == "file_transfer"
    assert row["message"] == "denied"


def test_id_whitelist_alarm_and_conn_audit_ref_are_accepted(admin_client):
    """typ 10 exists on master only; conn_audit_ref is sent for the two
    whitelist types and is ignored."""
    _register(admin_client)
    info = json.dumps({"id": "777", "ip": "203.0.113.53", "name": "Eve"})
    r = _alarm(admin_client, typ=10, conn_id=6, info=info, conn_audit_ref="opaque-ref", nonce="n10")
    assert r.status_code == 200
    assert _logs(admin_client)["items"][0]["alarm_type"] == 10


def test_a_1_4_9_payload_without_a_nonce_is_accepted(admin_client):
    _register(admin_client)
    r = _alarm(admin_client, typ=1, conn_id=1, info=json.dumps({"ip": "203.0.113.54"}))
    assert r.status_code == 200
    assert _logs(admin_client)["total"] == 1


def test_a_retry_with_the_same_nonce_is_stored_once(admin_client):
    """CLAUDE.md section 43: the client retries transport errors and 5xx with
    the same body."""
    _register(admin_client)
    for _ in range(3):
        _alarm(admin_client, typ=1, conn_id=1, info="{}", nonce="same")
    assert _logs(admin_client)["total"] == 1


def test_unknown_device_is_dropped_but_answers_identically(admin_client):
    """So the unauthenticated endpoint cannot be used to probe device ids."""
    r = admin_client.post("/api/audit/alarm", json={"id": "nobody", "typ": 0, "info": "{}"})
    assert r.status_code == 200 and r.content == b""
    assert _logs(admin_client)["total"] == 0


def test_uuid_mismatch_and_missing_uuid_are_dropped(admin_client):
    _register(admin_client, uuid="real-uuid")
    admin_client.post("/api/audit/alarm", json={"id": "dev-a", "uuid": "forged", "typ": 0, "info": "{}"})
    admin_client.post("/api/audit/alarm", json={"id": "dev-a", "typ": 0, "info": "{}"})
    assert _logs(admin_client)["total"] == 0


def test_missing_id_returns_error(client):
    r = client.post("/api/audit/alarm", json={"typ": 0})
    assert r.status_code == 200 and "error" in r.json()


def test_strings_are_capped_and_only_known_info_keys_are_stored(admin_client):
    _register(admin_client)
    info = json.dumps(
        {
            "ip": "1" * 500,
            "id": "i" * 500,
            "name": "n" * 5000,
            "conn_type": "c" * 500,
            "message": "m" * 5000,
            "smuggled": "x" * 100,
        }
    )
    _alarm(admin_client, typ=9, conn_id="9" * 100, info=info)

    row = _logs(admin_client)["items"][0]
    assert len(row["from_ip"]) == 64 and len(row["peer_id"]) == 64
    assert len(row["peer_name"]) == 255 and len(row["message"]) == 255
    assert len(row["conn_type"]) == 32
    assert "smuggled" not in json.dumps(row)


def test_malformed_info_and_unknown_type_are_tolerated(admin_client):
    _register(admin_client)
    assert _alarm(admin_client, typ=99, info="not json {").status_code == 200
    assert _alarm(admin_client, typ="weird", info=["not", "an", "object"]).status_code == 200
    # An object instead of the usual JSON-encoded string is accepted too.
    assert _alarm(admin_client, typ=0, info={"ip": "203.0.113.60"}).status_code == 200

    items = {r["alarm_type"]: r for r in _logs(admin_client)["items"]}
    assert set(items) == {99, None, 0}
    assert items[0]["from_ip"] == "203.0.113.60"


def test_alarm_endpoint_is_rate_limited_per_ip(client):
    client.app.state.client_audit_rate_limiter = RateLimiter(max_attempts=2, window_seconds=60)
    codes = [client.post("/api/audit/alarm", json={"id": "x"}).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]
