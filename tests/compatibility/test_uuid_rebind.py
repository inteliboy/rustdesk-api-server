"""A device id that is already known must not be taken over by an anonymous
sysinfo upload that carries a different `uuid`.

`/api/sysinfo` and `/api/heartbeat` are unauthenticated in the RustDesk client
protocol, and a RustDesk id is a 9-digit number, so the uuid the client
generated is the only thing tying a heartbeat (policy, disconnects) to a device.
DEVICE_UUID_REBIND decides what happens to a mismatch; an upload carrying the
login token of the owner or an administrator is trusted.
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
    clear_settings_cache()


def _sysinfo(client, uuid=UUID, hostname="real-host", rustdesk_id="1001", token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = client.post(
        "/api/sysinfo",
        json={"id": rustdesk_id, "uuid": uuid, "hostname": hostname, "os": "linux / x"},
        headers=headers,
    )
    assert (r.status_code, r.text) == (200, "SYSINFO_UPDATED")


def _device(client, rustdesk_id="1001"):
    items = client.get("/api/v1/devices").json()["items"]
    return next(d for d in items if d["rustdesk_id"] == rustdesk_id)


def _beat(client, uuid=UUID, rustdesk_id="1001"):
    return client.post("/api/heartbeat", json={"id": rustdesk_id, "uuid": uuid}).json()


def _client_token(app, username, password, **body):
    r = TestClient(app).post("/api/login", json={"username": username, "password": password, **body})
    assert "access_token" in r.json(), r.text
    return r.json()["access_token"]


def _app_with(monkeypatch, mode):
    monkeypatch.setenv("DEVICE_UUID_REBIND", mode)
    clear_settings_cache()
    from rustdesk_api.app import create_app

    return create_app()


def test_a_different_uuid_is_parked_and_changes_nothing(admin_client):
    _sysinfo(admin_client)
    before = _device(admin_client)

    _sysinfo(admin_client, uuid=OTHER, hostname="attacker-host")

    after = _device(admin_client)
    assert after["hostname"] == "real-host"
    assert after["uuid_change_pending"] is True
    assert after["last_seen"] == before["last_seen"]  # the stranger did not make it look alive
    assert "uuid" not in after and OTHER not in str(after)
    # The real install is still the one the server listens to; the stranger gets nothing.
    assert _beat(admin_client) == {"data": "OK"}
    assert _beat(admin_client, uuid=OTHER) == {"data": "OK"}
    assert _device(admin_client)["last_seen"] != before["last_seen"]


def test_an_intruder_heartbeat_does_not_refresh_last_seen(admin_client):
    _sysinfo(admin_client)
    seen = _device(admin_client)["last_seen"]
    _beat(admin_client, uuid=OTHER)
    assert _device(admin_client)["last_seen"] == seen


def test_the_owner_can_accept_the_new_install(admin_client):
    _sysinfo(admin_client)
    _sysinfo(admin_client, uuid=OTHER, hostname="reinstalled")
    device = _device(admin_client)

    r = admin_client.post(f"/api/v1/devices/{device['id']}/uuid/accept")
    assert (r.status_code, r.json()) == (200, {"pending": False})

    _sysinfo(admin_client, uuid=OTHER, hostname="reinstalled")
    now = _device(admin_client)
    assert (now["hostname"], now["uuid_change_pending"]) == ("reinstalled", False)
    # The new install now owns the identity; the old one is the stranger.
    strategy_probe = admin_client.post(
        "/api/heartbeat", json={"id": "1001", "uuid": UUID, "conns": [7]}
    ).json()
    assert strategy_probe == {"data": "OK"}
    assert admin_client.get(f"/api/v1/devices/{device['id']}/connections").json() == []


def test_the_owner_can_reject_and_a_second_decision_is_a_conflict(admin_client):
    _sysinfo(admin_client)
    _sysinfo(admin_client, uuid=OTHER)
    device = _device(admin_client)
    assert admin_client.post(f"/api/v1/devices/{device['id']}/uuid/reject").status_code == 200
    assert _device(admin_client)["uuid_change_pending"] is False
    assert admin_client.post(f"/api/v1/devices/{device['id']}/uuid/accept").status_code == 409


def _user_client(app, admin_client, name):
    r = admin_client.post("/api/v1/users", json={"username": name, "password": "userpassword1"})
    assert r.status_code == 201, r.text
    c = TestClient(app)
    c.post("/api/v1/auth/login", json={"username": name, "password": "userpassword1"})
    c.headers.update({"X-CSRF-Token": c.cookies.get("rd_csrf")})
    return c, r.json()["id"]


def test_only_the_owner_or_an_admin_can_decide(app, admin_client):
    alice, alice_id = _user_client(app, admin_client, "alice")
    bob, _ = _user_client(app, admin_client, "bob")
    _sysinfo(admin_client)
    _sysinfo(admin_client, uuid=OTHER)
    device = _device(admin_client)

    # Not visible to alice at all.
    assert alice.post(f"/api/v1/devices/{device['id']}/uuid/accept").status_code == 404

    admin_client.patch(f"/api/v1/devices/{device['id']}", json={"owner_id": alice_id})
    # Bob is unrelated; a share, even with control, is not enough either.
    assert bob.post(f"/api/v1/devices/{device['id']}/uuid/accept").status_code == 404
    shared = admin_client.post(
        f"/api/v1/devices/{device['id']}/shares", json={"username": "bob", "permission": "control"}
    )
    assert shared.status_code == 201, shared.text
    assert bob.get(f"/api/v1/devices/{device['id']}").status_code == 200  # bob can see it now ...
    assert bob.post(f"/api/v1/devices/{device['id']}/uuid/accept").status_code == 404  # ... but not decide
    assert alice.post(f"/api/v1/devices/{device['id']}/uuid/accept").status_code == 200


def test_an_upload_with_the_owners_or_an_admins_token_is_trusted(app, admin_client):
    _sysinfo(admin_client)
    token = _client_token(app, "admin", "adminpass123")
    _sysinfo(admin_client, uuid=OTHER, hostname="reinstalled", token=token)
    device = _device(admin_client)
    assert (device["hostname"], device["uuid_change_pending"]) == ("reinstalled", False)
    assert _beat(admin_client, uuid=OTHER) == {"data": "OK"}


def test_someone_elses_token_does_not_make_the_upload_trusted(app, admin_client):
    alice, _ = _user_client(app, admin_client, "alice")
    _sysinfo(admin_client)
    token = _client_token(app, "alice", "userpassword1")
    _sysinfo(admin_client, uuid=OTHER, hostname="hijack", token=token)
    device = _device(admin_client)
    assert (device["hostname"], device["uuid_change_pending"]) == ("real-host", True)
    assert device["owner_id"] is None  # and alice did not get the device either


def test_signing_in_from_the_client_rebinds_only_a_device_the_user_owns(app, admin_client):
    alice, alice_id = _user_client(app, admin_client, "alice")
    _sysinfo(admin_client)
    device = _device(admin_client)

    # An unowned device: a login from another install must not take it over.
    _client_token(app, "alice", "userpassword1", id="1001", uuid=OTHER)
    after = _device(admin_client)
    assert after["uuid_change_pending"] is True and after["owner_id"] is None

    admin_client.post(f"/api/v1/devices/{device['id']}/uuid/reject")
    admin_client.patch(f"/api/v1/devices/{device['id']}", json={"owner_id": alice_id})
    _client_token(app, "alice", "userpassword1", id="1001", uuid=OTHER)
    assert _device(admin_client)["uuid_change_pending"] is False
    assert _beat(admin_client, uuid=OTHER) == {"data": "OK"}


def test_the_attempt_is_on_the_timeline_and_in_the_audit_log(admin_client):
    _sysinfo(admin_client)
    _sysinfo(admin_client, uuid=OTHER)
    _sysinfo(admin_client, uuid=OTHER)  # the client retries: still one entry
    device = _device(admin_client)

    timeline = admin_client.get(f"/api/v1/devices/{device['id']}/timeline").json()
    requested = [e for e in timeline if e["kind"] == "uuid_change_requested"]
    assert len(requested) == 1
    audit = admin_client.get("/api/v1/admin/audit-logs").json()["items"]
    assert [e["action"] for e in audit].count("device_uuid_change_requested") == 1


def test_deny_ignores_the_upload_and_allow_adopts_it(admin_client, monkeypatch):
    deny = TestClient(_app_with(monkeypatch, "deny"))
    _sysinfo(deny)
    _sysinfo(deny, uuid=OTHER, hostname="x")
    # (a fresh app on the same database, so log in again to read the device)
    deny.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"})
    assert _device(deny)["uuid_change_pending"] is False
    assert _device(deny)["hostname"] == "real-host"

    allow = TestClient(_app_with(monkeypatch, "allow"))
    _sysinfo(allow, uuid=OTHER, hostname="adopted")
    allow.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"})
    assert _device(allow)["hostname"] == "adopted"
    assert _beat(allow, uuid=OTHER) == {"data": "OK"}


def test_an_unknown_id_and_a_first_upload_are_unaffected(admin_client):
    _sysinfo(admin_client, rustdesk_id="2002", uuid=OTHER)
    assert _device(admin_client, "2002")["uuid_change_pending"] is False
    _sysinfo(admin_client, rustdesk_id="2002", uuid=OTHER, hostname="renamed")
    assert _device(admin_client, "2002")["hostname"] == "renamed"


def test_an_invalid_policy_is_a_startup_error(monkeypatch):
    from pydantic import ValidationError

    from rustdesk_api.config import Settings

    monkeypatch.setenv("DEVICE_UUID_REBIND", "sometimes")
    with pytest.raises(ValidationError):
        Settings()
