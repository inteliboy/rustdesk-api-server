"""The note a controlling user can leave when a session ends.

Shapes are taken from the RustDesk client source (1.4.9 and master), NOT yet
seen from a live client: the controlling client asks
`GET /api/audit/conn/active?id=&session_id=&conn_type=` (bearer token) for a GUID
and later `PUT /api/audit` with `{guid, note}`; the older Sciter client instead
posts `{id, session_id, note}` to `/api/audit/conn` with no token and no uuid.
The controlled device's own reports (`/api/audit/conn`) create the session row.
"""

import datetime

from fastapi.testclient import TestClient

from rustdesk_api.db.database import get_session_factory
from rustdesk_api.models.client_audit import ConnectionLog

SESSION = 5550001


def _token(client, username="admin", password="adminpass123"):
    return client.post("/api/login", json={"username": username, "password": password}).json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _session(client, session_id=SESSION, conn_type=0):
    """A device that reported one authorized session, as the controlled client does."""
    client.post("/api/sysinfo", json={"id": "dev-a", "uuid": "uuid-a", "hostname": "h", "os": "windows"})
    base = {"id": "dev-a", "uuid": "uuid-a", "conn_id": 5, "session_id": session_id}
    client.post("/api/audit/conn", json={**base, "nonce": "n1", "action": "new", "ip": "203.0.113.7"})
    client.post("/api/audit/conn", json={**base, "nonce": "n2", "peer": ["42", "Bob"], "type": conn_type})


def _guid(client, token, session_id=SESSION, conn_type=0):
    r = client.get(
        "/api/audit/conn/active",
        params={"id": "dev-a", "session_id": session_id, "conn_type": conn_type},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _put(client, token, **body):
    return client.put("/api/audit", json=body, headers=_auth(token) if token else {})


def _row(admin_client):
    return admin_client.get("/api/v1/connection-logs").json()["items"][0]


def test_the_controlling_client_gets_a_guid_and_its_note_lands_on_the_session(admin_client):
    _session(admin_client)
    token = _token(admin_client)

    guid = _guid(admin_client, token)
    assert isinstance(guid, str) and len(guid) == 36
    assert _guid(admin_client, token) == guid  # asking again does not make another

    r = _put(admin_client, token, guid=guid, note="  Fixed the printer  ")
    assert r.status_code == 200 and r.content == b""
    assert _row(admin_client)["note"] == "Fixed the printer"


def test_the_note_is_on_the_device_timeline(admin_client):
    _session(admin_client)
    token = _token(admin_client)
    _put(admin_client, token, guid=_guid(admin_client, token), note="Rebooted")

    device = admin_client.get("/api/v1/devices").json()["items"][0]
    timeline = admin_client.get(f"/api/v1/devices/{device['id']}/timeline").json()
    connections = [e for e in timeline if e["source"] == "connection"]
    assert connections[0]["detail"]["note"] == "Rebooted"


def test_the_guid_is_never_shown_by_the_management_api(admin_client):
    _session(admin_client)
    guid = _guid(admin_client, _token(admin_client))
    assert guid not in admin_client.get("/api/v1/connection-logs").text


def test_no_guid_until_the_controlled_device_has_reported_the_session(admin_client):
    admin_client.post(
        "/api/sysinfo", json={"id": "dev-a", "uuid": "uuid-a", "hostname": "h", "os": "windows"}
    )
    # JSON null: the client asks again a few times.
    assert _guid(admin_client, _token(admin_client)) is None


def test_the_connection_type_must_match_when_the_session_reported_one(admin_client):
    _session(admin_client, conn_type=1)  # file transfer
    token = _token(admin_client)
    assert _guid(admin_client, token, conn_type=0) is None
    assert _guid(admin_client, token, conn_type=1) is not None


def test_a_session_id_of_another_session_gets_nothing(admin_client):
    _session(admin_client)
    assert _guid(admin_client, _token(admin_client), session_id=SESSION + 1) is None


def test_both_calls_need_a_signed_in_client(admin_client):
    _session(admin_client)
    guid = _guid(admin_client, _token(admin_client))
    anonymous = TestClient(admin_client.app)

    r = anonymous.get("/api/audit/conn/active", params={"id": "dev-a", "session_id": SESSION})
    assert r.status_code == 401
    assert _put(anonymous, None, guid=guid, note="x").status_code == 401
    assert _put(anonymous, "not-a-token", guid=guid, note="x").status_code == 401
    assert _row(admin_client)["note"] is None


def test_an_api_key_is_not_a_client_token(admin_client):
    _session(admin_client)
    created = admin_client.post("/api/v1/api-keys", json={"label": "script", "scope": "full"})
    key = created.json()["token"]
    r = TestClient(admin_client.app).get(
        "/api/audit/conn/active", params={"id": "dev-a", "session_id": SESSION}, headers=_auth(key)
    )
    assert r.status_code == 401


def test_an_unknown_guid_is_a_404_and_changes_nothing(admin_client):
    _session(admin_client)
    token = _token(admin_client)
    assert (
        _put(admin_client, token, guid="0" * 8 + "-0000-0000-0000-" + "0" * 12, note="x").status_code == 404
    )
    assert _put(admin_client, token, note="x").status_code == 404
    assert _row(admin_client)["note"] is None


def test_an_empty_or_oversized_note_is_handled(admin_client):
    _session(admin_client)
    token = _token(admin_client)
    guid = _guid(admin_client, token)

    assert _put(admin_client, token, guid=guid, note="   ").status_code == 404
    _put(admin_client, token, guid=guid, note="a\x00b" + "x" * 5000)
    note = _row(admin_client)["note"]
    assert len(note) == 1000 and "\x00" not in note


def test_a_session_that_ended_long_ago_takes_no_note(admin_client):
    _session(admin_client)
    token = _token(admin_client)
    guid = _guid(admin_client, token)
    with get_session_factory()() as db:
        row = db.query(ConnectionLog).one()
        row.ended_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
        db.commit()

    assert _put(admin_client, token, guid=guid, note="late").status_code == 404
    assert _guid(admin_client, token) is None


def test_the_older_client_posts_its_note_with_the_session_id(admin_client):
    _session(admin_client)
    anonymous = TestClient(admin_client.app)  # that client sends no token and no uuid

    r = anonymous.post("/api/audit/conn", json={"id": "dev-a", "session_id": SESSION, "note": "Old note"})
    assert r.status_code == 200 and r.content == b""
    assert _row(admin_client)["note"] == "Old note"

    # The wrong pair changes nothing, and answers the same.
    r = anonymous.post("/api/audit/conn", json={"id": "dev-a", "session_id": SESSION + 1, "note": "guess"})
    assert r.status_code == 200 and r.content == b""
    r = anonymous.post("/api/audit/conn", json={"id": "nobody", "session_id": SESSION, "note": "guess"})
    assert r.status_code == 200
    assert _row(admin_client)["note"] == "Old note"
