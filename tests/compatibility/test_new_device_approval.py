"""NEW_DEVICE_POLICY=approve: a device id this server has never seen is recorded as
pending and given nothing until an administrator approves it.

The sysinfo and heartbeat endpoints are unauthenticated in the RustDesk client
protocol, so without this any machine that can reach the server is registered and
handed policy. A device that arrives with an administrator's login is vouched for.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rustdesk_api.config import clear_settings_cache

UUID = "M2Y5YzJhNzEtNmI0ZC00ZTA4LWE1ZDMtOWMxZTdiMjBmODQ2"
OTHER = "b3RoZXItbWFjaGluZS1vdGhlci1tYWNoaW5l"


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("AUTH_RATE_LIMIT_ATTEMPTS", "1000")
    monkeypatch.setenv("NEW_DEVICE_POLICY", "approve")
    clear_settings_cache()


def _sysinfo(client, rustdesk_id="1001", uuid=UUID, hostname="new-host", token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = client.post(
        "/api/sysinfo",
        json={"id": rustdesk_id, "uuid": uuid, "hostname": hostname, "os": "linux / x"},
        headers=headers,
    )
    assert (r.status_code, r.text) == (200, "SYSINFO_UPDATED")


def _beat(client, rustdesk_id="1001", uuid=UUID, **extra):
    return client.post("/api/heartbeat", json={"id": rustdesk_id, "uuid": uuid, **extra}).json()


def _listed(client, status=None):
    query = f"?status={status}" if status else ""
    return client.get(f"/api/v1/devices{query}").json()["items"]


def _pending(client, rustdesk_id="1001"):
    return next((d for d in _listed(client, "pending") if d["rustdesk_id"] == rustdesk_id), None)


def _client_token(app, username, password, **body):
    r = TestClient(app).post("/api/login", json={"username": username, "password": password, **body})
    assert "access_token" in r.json(), r.text
    return r.json()["access_token"]


def _user_client(app, admin_client, name):
    r = admin_client.post("/api/v1/users", json={"username": name, "password": "userpassword1"})
    assert r.status_code == 201, r.text
    c = TestClient(app)
    c.post("/api/v1/auth/login", json={"username": name, "password": "userpassword1"})
    c.headers.update({"X-CSRF-Token": c.cookies.get("rd_csrf")})
    return c, r.json()["id"]


def test_the_default_policy_registers_a_device_at_once(admin_client, monkeypatch):
    monkeypatch.setenv("NEW_DEVICE_POLICY", "allow")
    clear_settings_cache()
    _sysinfo(admin_client)
    assert [d["approval"] for d in _listed(admin_client)] == ["approved"]


def test_an_unknown_device_waits_and_is_not_counted(admin_client):
    _sysinfo(admin_client)

    assert _listed(admin_client) == []
    waiting = _pending(admin_client)
    assert waiting is not None
    # What an administrator needs to recognise it is kept.
    assert (waiting["hostname"], waiting["approval"]) == ("new-host", "pending")
    stats = admin_client.get("/api/v1/admin/dashboard").json()
    assert (stats["total_devices"], stats["online_devices"], stats["pending_devices"]) == (0, 0, 1)

    from rustdesk_api.config import get_settings
    from rustdesk_api.db.database import get_session_factory
    from rustdesk_api.services import metrics

    with get_session_factory()() as db:
        text = metrics.render(db, get_settings(), metrics.Counters())
    assert "rustdesk_api_devices_pending_approval 1" in text
    assert "rustdesk_api_devices 0.0" in text or "rustdesk_api_devices 0" in text


def test_a_waiting_device_gets_no_policy_and_no_say(admin_client):
    _sysinfo(admin_client)
    device = _pending(admin_client)
    seen = device["last_seen"]

    assert _beat(admin_client, conns=[7]) == {"data": "OK"}
    assert admin_client.get(f"/api/v1/devices/{device['id']}/connections").json() == []
    # It is still alive as far as the person deciding is concerned.
    assert _pending(admin_client)["last_seen"] >= seen

    # Its audit events are not accepted either.
    r = admin_client.post(
        "/api/audit/conn", json={"id": "1001", "uuid": UUID, "conn_id": 1, "action": "new", "ip": "1.2.3.4"}
    )
    assert r.status_code == 200
    assert admin_client.get("/api/v1/connection-logs").json()["total"] == 0


def test_approving_makes_it_an_ordinary_device(admin_client):
    _sysinfo(admin_client)
    device = _pending(admin_client)

    r = admin_client.post(f"/api/v1/devices/{device['id']}/approve")
    assert (r.status_code, r.json()) == (200, {"approval": "approved"})

    assert [d["rustdesk_id"] for d in _listed(admin_client)] == ["1001"]
    assert _pending(admin_client) is None
    _beat(admin_client, conns=[7])
    assert [c["id"] for c in admin_client.get(f"/api/v1/devices/{device['id']}/connections").json()] == [7]
    assert admin_client.post(f"/api/v1/devices/{device['id']}/approve").status_code == 409
    kinds = [e["kind"] for e in admin_client.get(f"/api/v1/devices/{device['id']}/timeline").json()]
    assert "approved" in kinds
    actions = [e["action"] for e in admin_client.get("/api/v1/admin/audit-logs").json()["items"]]
    assert "device_approved" in actions and "device_registration_pending" in actions


def test_a_rejected_device_stays_on_record_and_is_ignored(admin_client):
    _sysinfo(admin_client)
    device = _pending(admin_client)
    assert admin_client.post(f"/api/v1/devices/{device['id']}/reject").status_code == 200

    _sysinfo(admin_client, hostname="changed-its-mind")
    assert _beat(admin_client, conns=[7]) == {"data": "OK"}

    assert _pending(admin_client) is None
    rejected = _listed(admin_client, "rejected")
    assert [(d["rustdesk_id"], d["hostname"]) for d in rejected] == [("1001", "new-host")]
    assert _listed(admin_client) == []
    # Rejecting is not final: an administrator can still approve it.
    assert admin_client.post(f"/api/v1/devices/{device['id']}/approve").status_code == 200
    assert admin_client.post(f"/api/v1/devices/{device['id']}/reject").status_code == 200
    assert admin_client.post(f"/api/v1/devices/{device['id']}/reject").status_code == 409


def test_bulk_approve_and_reject(admin_client):
    for n in ("1001", "1002", "1003"):
        _sysinfo(admin_client, rustdesk_id=n)
    ids = [d["id"] for d in _listed(admin_client, "pending")]
    assert len(ids) == 3

    r = admin_client.post("/api/v1/devices/bulk", json={"ids": ids[:2], "action": "approve"})
    assert sorted(r.json()["updated"]) == sorted(ids[:2])
    r = admin_client.post("/api/v1/devices/bulk", json={"ids": [ids[0], ids[2]], "action": "reject"})
    assert r.json()["updated"] == [ids[0], ids[2]]
    r = admin_client.post("/api/v1/devices/bulk", json={"ids": [ids[2]], "action": "reject"})
    assert r.json() == {"updated": [], "skipped": [{"id": ids[2], "reason": "unchanged"}]}
    assert sorted(d["id"] for d in _listed(admin_client, "rejected")) == sorted([ids[0], ids[2]])
    assert [d["id"] for d in _listed(admin_client)] == [ids[1]]


def test_an_administrators_login_vouches_for_a_device(app, admin_client):
    token = _client_token(app, "admin", "adminpass123")
    _sysinfo(admin_client, rustdesk_id="2001", token=token)
    assert [d["rustdesk_id"] for d in _listed(admin_client)] == ["2001"]

    # Signing in from the client with the device's id registers it the same way.
    _client_token(app, "admin", "adminpass123", id="2002", uuid=OTHER)
    assert sorted(d["rustdesk_id"] for d in _listed(admin_client)) == ["2001", "2002"]

    # A device that already waited is approved when an administrator signs in from it.
    _sysinfo(admin_client, rustdesk_id="2003")
    assert _pending(admin_client, "2003") is not None
    _client_token(app, "admin", "adminpass123", id="2003", uuid=UUID)
    assert _pending(admin_client, "2003") is None
    assert "2003" in [d["rustdesk_id"] for d in _listed(admin_client)]


def test_an_ordinary_users_login_does_not(app, admin_client):
    alice, alice_id = _user_client(app, admin_client, "alice")
    token = _client_token(app, "alice", "userpassword1")
    _sysinfo(admin_client, rustdesk_id="3001", token=token)
    _client_token(app, "alice", "userpassword1", id="3002", uuid=OTHER)

    waiting = sorted(d["rustdesk_id"] for d in _listed(admin_client, "pending"))
    assert waiting == ["3001", "3002"]
    # Alice's account works, but the devices are not hers to see or open until approved.
    assert alice.get("/api/v1/devices").json()["items"] == []
    device = _pending(admin_client, "3001")
    assert device["owner_id"] == alice_id
    assert alice.get(f"/api/v1/devices/{device['id']}").status_code == 404
    admin_client.post(f"/api/v1/devices/{device['id']}/approve")
    assert alice.get(f"/api/v1/devices/{device['id']}").status_code == 200


def test_only_administrators_decide_or_list(app, admin_client):
    alice, _ = _user_client(app, admin_client, "alice")
    _sysinfo(admin_client)
    device = _pending(admin_client)

    assert alice.get("/api/v1/devices?status=pending").status_code == 403
    assert alice.get("/api/v1/devices?status=rejected").status_code == 403
    assert alice.post(f"/api/v1/devices/{device['id']}/approve").status_code == 403
    assert alice.post(f"/api/v1/devices/{device['id']}/reject").status_code == 403
    # Not through the bulk endpoint either, and the device does not even show as existing.
    r = alice.post("/api/v1/devices/bulk", json={"ids": [device["id"]], "action": "approve"})
    assert r.json() == {"updated": [], "skipped": [{"id": device["id"], "reason": "not_found"}]}
    assert alice.get(f"/api/v1/devices/{device['id']}").status_code == 404
    assert _pending(admin_client) is not None


def test_only_so_many_devices_may_wait(admin_client, monkeypatch):
    monkeypatch.setenv("NEW_DEVICE_PENDING_LIMIT", "2")
    clear_settings_cache()
    for n in ("1", "2", "3"):
        _sysinfo(admin_client, rustdesk_id=f"400{n}")  # the third is answered like the others ...
    assert sorted(d["rustdesk_id"] for d in _listed(admin_client, "pending")) == [
        "4001",
        "4002",
    ]  # ... but not kept
    # A device already waiting can still update itself, and deciding frees a place.
    _sysinfo(admin_client, rustdesk_id="4001", hostname="renamed")
    assert _pending(admin_client, "4001")["hostname"] == "renamed"
    admin_client.post(f"/api/v1/devices/{_pending(admin_client, '4001')['id']}/approve")
    _sysinfo(admin_client, rustdesk_id="4003")
    assert _pending(admin_client, "4003") is not None


def test_presets_do_not_apply_to_a_waiting_device(admin_client, monkeypatch):
    monkeypatch.setenv("ALLOW_SYSINFO_PRESETS", "true")
    clear_settings_cache()

    def upload(rustdesk_id):
        body = {"id": rustdesk_id, "uuid": UUID, "hostname": "h", "os": "linux / x"}
        r = admin_client.post("/api/sysinfo", json={**body, "preset-note": "from the installer"})
        assert r.status_code == 200

    upload("5001")
    assert _pending(admin_client, "5001")["note"] is None

    # The same upload does place a device when nothing has to be approved.
    monkeypatch.setenv("NEW_DEVICE_POLICY", "allow")
    clear_settings_cache()
    upload("5002")
    placed = next(d for d in _listed(admin_client) if d["rustdesk_id"] == "5002")
    assert placed["note"] == "from the installer"


def test_the_browser_client_is_not_offered_for_a_waiting_device(admin_client):
    from rustdesk_api.models.device import Device
    from rustdesk_api.models.user import User
    from rustdesk_api.services import webclient as webclient_service

    admin = User(username="a", password_hash="x", is_admin=True, is_active=True)
    device = Device(rustdesk_id="1", approval="pending")
    assert webclient_service.can_connect(admin, device) is False
    device.approval = "approved"
    assert webclient_service.can_connect(admin, device) is True


def test_enrollment_by_an_administrator_approves_by_a_user_does_not(app, admin_client):
    def token(client):
        r = client.post("/api/v1/enrollment-tokens", json={"label": "t", "days": 1})
        assert r.status_code == 201, r.text
        return r.json()["token"]

    def assign(tok, rustdesk_id):
        return admin_client.post(
            "/api/devices/cli",
            json={"id": rustdesk_id, "uuid": UUID, "user_name": "admin"},
            headers={"Authorization": f"Bearer {tok}"},
        )

    r = assign(token(admin_client), "6001")
    assert r.status_code == 200, r.text
    assert "6001" in [d["rustdesk_id"] for d in _listed(admin_client)]

    alice, _ = _user_client(app, admin_client, "alice")
    r = assign(token(alice), "6002")
    # Alice may not assign to `admin`; what matters is that it did not become approved.
    assert "6002" not in [d["rustdesk_id"] for d in _listed(admin_client)]


def test_an_invalid_policy_is_a_startup_error(monkeypatch):
    from pydantic import ValidationError

    from rustdesk_api.config import Settings

    monkeypatch.setenv("NEW_DEVICE_POLICY", "maybe")
    with pytest.raises(ValidationError):
        Settings()
