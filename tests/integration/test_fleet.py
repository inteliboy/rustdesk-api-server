"""Default strategy, offline alerts, archived devices, outdated clients and the new-device count."""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from rustdesk_api.config import clear_settings_cache, get_settings
from rustdesk_api.db.database import get_session_factory
from rustdesk_api.models.device import Device
from rustdesk_api.services import fleet, notifications

UUID = "M2Y5YzJhNzEtNmI0ZC00ZTA4LWE1ZDMtOWMxZTdiMjBmODQ2"


def _register(client, rustdesk_id="1001", uuid=UUID, **extra):
    r = client.post(
        "/api/sysinfo", json={"id": rustdesk_id, "uuid": uuid, "hostname": "pc", "os": "linux / x", **extra}
    )
    assert r.status_code == 200
    return next(d for d in client.get("/api/v1/devices").json()["items"] if d["rustdesk_id"] == rustdesk_id)


def _beat(client, rustdesk_id="1001", uuid=UUID, **extra):
    r = client.post("/api/heartbeat", json={"id": rustdesk_id, "uuid": uuid, **extra})
    assert r.status_code == 200
    return r.json()


def _strategy(admin_client, name="Baseline", options=None):
    options = {"enable-file-transfer": "N"} if options is None else options
    r = admin_client.post("/api/v1/strategies", json={"name": name, "options": options})
    assert r.status_code == 201, r.text
    return r.json()


def _set_default(admin_client, strategy_id, is_default=True):
    r = admin_client.put(f"/api/v1/strategies/{strategy_id}/default", json={"is_default": is_default})
    assert r.status_code == 200, r.text
    return r.json()


def _age(rustdesk_id, **delta):
    """Move a device's last_seen (and creation) into the past."""
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(**delta)
    with get_session_factory()() as db:
        device = db.query(Device).filter_by(rustdesk_id=rustdesk_id).one()
        device.last_seen = when
        device.created_at = min(device.created_at, when) if device.created_at.tzinfo else when
        db.commit()


@pytest.fixture()
def alerts(monkeypatch):
    """Every notification the code asks for."""
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        notifications, "dispatch", lambda event, title, message, **_k: seen.append((event, message)) or True
    )
    return seen


# --- default strategy ---------------------------------------------------------


def test_a_device_with_no_strategy_of_its_own_gets_the_default(admin_client):
    device = _register(admin_client)
    baseline = _strategy(admin_client)
    assert _beat(admin_client, modified_at=0) == {"data": "OK"}

    marked = _set_default(admin_client, baseline["id"])
    assert marked["is_default"] is True

    pushed = _beat(admin_client, modified_at=0)
    assert pushed["modified_at"] == baseline["modified_at"]
    assert pushed["strategy"]["config_options"]["enable-file-transfer"] == "N"
    # The client stored it: quiet from now on.
    assert _beat(admin_client, modified_at=pushed["modified_at"]) == {"data": "OK"}
    assert admin_client.get(f"/api/v1/devices/{device['id']}").json()["effective_strategy_name"] == "Baseline"


def test_a_devices_own_strategy_and_its_groups_win_over_the_default(admin_client):
    device = _register(admin_client)
    baseline = _strategy(admin_client)
    own = _strategy(admin_client, "Own", {"enable-clipboard": "N"})
    _set_default(admin_client, baseline["id"])

    r = admin_client.put(f"/api/v1/devices/{device['id']}/strategy", json={"strategy_id": own["id"]})
    assert r.status_code == 200
    assert _beat(admin_client, modified_at=0)["modified_at"] == own["modified_at"]

    group = admin_client.post("/api/v1/groups", json={"name": "Servers"}).json()
    group_strategy = _strategy(admin_client, "Group", {"enable-audio": "N"})
    admin_client.put(f"/api/v1/groups/{group['id']}/strategy", json={"strategy_id": group_strategy["id"]})
    admin_client.put(f"/api/v1/devices/{device['id']}/strategy", json={"strategy_id": None})
    admin_client.patch(f"/api/v1/devices/{device['id']}", json={"group_id": group["id"]})
    assert _beat(admin_client, modified_at=0)["modified_at"] == group_strategy["modified_at"]


def test_only_one_strategy_is_the_default(admin_client):
    first = _strategy(admin_client, "First")
    second = _strategy(admin_client, "Second", {"enable-clipboard": "N"})
    _set_default(admin_client, first["id"])
    _set_default(admin_client, second["id"])

    flags = {s["name"]: s["is_default"] for s in admin_client.get("/api/v1/strategies").json()}
    assert flags == {"First": False, "Second": True}


def test_clearing_the_default_resets_the_devices_that_used_it(admin_client):
    _register(admin_client)
    baseline = _strategy(admin_client)
    _set_default(admin_client, baseline["id"])
    pushed = _beat(admin_client, modified_at=0)

    _set_default(admin_client, baseline["id"], False)

    reset = _beat(admin_client, modified_at=pushed["modified_at"])
    assert reset["modified_at"] == 0


def test_clearing_a_strategy_that_is_not_the_default_leaves_the_default_alone(admin_client):
    first = _strategy(admin_client, "First")
    second = _strategy(admin_client, "Second", {"enable-clipboard": "N"})
    _set_default(admin_client, first["id"])
    _set_default(admin_client, second["id"], False)
    assert {s["name"]: s["is_default"] for s in admin_client.get("/api/v1/strategies").json()}[
        "First"
    ] is True


def test_deleting_the_default_leaves_no_default(admin_client):
    baseline = _strategy(admin_client)
    _set_default(admin_client, baseline["id"])
    assert admin_client.delete(f"/api/v1/strategies/{baseline['id']}").status_code == 204
    _register(admin_client)
    assert _beat(admin_client, modified_at=0) == {"data": "OK"}


def test_only_an_administrator_can_set_the_default(app, admin_client):
    baseline = _strategy(admin_client)
    admin_client.post("/api/v1/users", json={"username": "bob", "password": "userpassword1"})
    bob = TestClient(app)
    bob.post("/api/v1/auth/login", json={"username": "bob", "password": "userpassword1"})
    bob.headers.update({"X-CSRF-Token": bob.cookies.get("rd_csrf")})
    assert (
        bob.put(f"/api/v1/strategies/{baseline['id']}/default", json={"is_default": True}).status_code == 403
    )
    assert admin_client.put("/api/v1/strategies/999/default", json={"is_default": True}).status_code == 404


def test_the_dashboard_counts_devices_that_would_get_no_strategy(admin_client):
    _register(admin_client, "1001")
    _register(admin_client, "1002", uuid="other-uuid-2")
    assert admin_client.get("/api/v1/admin/dashboard").json()["devices_without_strategy"] == 2

    _set_default(admin_client, _strategy(admin_client)["id"])
    assert admin_client.get("/api/v1/admin/dashboard").json()["devices_without_strategy"] == 0


# --- offline alerts -------------------------------------------------------------


def _watch(admin_client, device, watch=True):
    r = admin_client.put(f"/api/v1/devices/{device['id']}/watch", json={"watch": watch})
    assert r.status_code == 200, r.text


def _outage_alerts(alerts):
    """The offline/back-online alerts (registering a device raises new_device as well)."""
    return [event for event, _message in alerts if event != "new_device"]


def _check(**kwargs):
    with get_session_factory()() as db:
        return fleet.check_watched_devices(db, get_settings(), **kwargs)


def test_a_watched_device_that_goes_quiet_is_announced_once(admin_client, alerts):
    device = _register(admin_client)
    _watch(admin_client, device)
    assert _check() == fleet.WatchResult(0, 0)  # still reporting

    _age("1001", minutes=30)
    assert _check() == fleet.WatchResult(1, 0)
    assert _outage_alerts(alerts) == ["device_offline"] and "1001" in alerts[-1][1]
    # The same outage is not announced again.
    assert _check() == fleet.WatchResult(0, 0)
    assert _outage_alerts(alerts) == ["device_offline"]


def test_it_is_announced_again_after_it_came_back_and_went_quiet_again(admin_client, alerts):
    device = _register(admin_client)
    _watch(admin_client, device)
    _age("1001", minutes=30)
    _check()

    _beat(admin_client)  # it is back
    assert _check() == fleet.WatchResult(0, 1)
    assert alerts[-1][0] == "device_back_online"

    _age("1001", minutes=30)
    assert _check() == fleet.WatchResult(1, 0)
    assert _outage_alerts(alerts) == ["device_offline", "device_back_online", "device_offline"]


def test_a_device_that_is_not_watched_is_never_announced(admin_client, alerts):
    _register(admin_client)
    _age("1001", days=3)
    assert _check() == fleet.WatchResult(0, 0)
    assert _outage_alerts(alerts) == []


def test_the_grace_period_is_at_least_the_online_timeout(admin_client, alerts, monkeypatch):
    device = _register(admin_client)
    _watch(admin_client, device)
    _age("1001", minutes=5)  # silent, but NOTIFY_OFFLINE_AFTER_MINUTES is 10
    assert _check() == fleet.WatchResult(0, 0)
    _age("1001", minutes=11)
    assert _check() == fleet.WatchResult(1, 0)


def test_unwatching_forgets_an_announced_outage(admin_client, alerts):
    device = _register(admin_client)
    _watch(admin_client, device)
    _age("1001", minutes=30)
    _check()
    _watch(admin_client, device, False)
    with get_session_factory()() as db:
        assert db.query(Device).one().offline_notified_at is None
    _watch(admin_client, device, True)
    assert _check() == fleet.WatchResult(1, 0)


def test_only_administrators_change_offline_alerts(app, admin_client):
    device = _register(admin_client)
    admin_client.post("/api/v1/users", json={"username": "bob", "password": "userpassword1"})
    bob = TestClient(app)
    bob.post("/api/v1/auth/login", json={"username": "bob", "password": "userpassword1"})
    bob.headers.update({"X-CSRF-Token": bob.cookies.get("rd_csrf")})
    assert bob.put(f"/api/v1/devices/{device['id']}/watch", json={"watch": True}).status_code == 403
    r = bob.post("/api/v1/devices/bulk", json={"ids": [device["id"]], "action": "watch"})
    assert r.status_code == 403


def test_watching_can_be_done_in_bulk_and_shows_on_the_device(admin_client):
    a = _register(admin_client, "1001")
    b = _register(admin_client, "1002", uuid="other-uuid-2")
    r = admin_client.post("/api/v1/devices/bulk", json={"ids": [a["id"], b["id"]], "action": "watch"})
    assert r.json()["updated"] == [a["id"], b["id"]]
    assert admin_client.get(f"/api/v1/devices/{a['id']}").json()["watch_offline"] is True
    admin_client.post("/api/v1/devices/bulk", json={"ids": [a["id"]], "action": "unwatch"})
    assert admin_client.get(f"/api/v1/devices/{a['id']}").json()["watch_offline"] is False


def test_new_devices_and_takeover_attempts_are_announced(admin_client, alerts):
    _register(admin_client, "1001")
    assert alerts[-1][0] == "new_device" and "1001" in alerts[-1][1]
    _register(admin_client, "1001")  # the same device again is not new
    assert [a[0] for a in alerts].count("new_device") == 1

    # Another install using the same id is parked, and that is announced.
    _register(admin_client, "1001", uuid="a-different-install")
    assert alerts[-1][0] == "device_takeover_attempt"


# --- archiving ---------------------------------------------------------------------


def _with_stale_days(monkeypatch, days):
    monkeypatch.setenv("DEVICE_STALE_DAYS", str(days))
    clear_settings_cache()
    return get_settings()


def _archive_stale(settings, **kwargs):
    with get_session_factory()() as db:
        return fleet.archive_stale(db, settings, **kwargs)


def test_a_device_silent_for_too_long_is_archived_and_leaves_the_default_list(admin_client, monkeypatch):
    settings = _with_stale_days(monkeypatch, 30)
    _register(admin_client, "1001")
    _register(admin_client, "1002", uuid="other-uuid-2")
    _age("1002", days=45)

    assert _archive_stale(settings) == 1

    listed = admin_client.get("/api/v1/devices").json()
    assert [d["rustdesk_id"] for d in listed["items"]] == ["1001"] and listed["total"] == 1
    archived = admin_client.get("/api/v1/devices?status=archived").json()
    assert [d["rustdesk_id"] for d in archived["items"]] == ["1002"]
    assert archived["items"][0]["archived"] is True
    # Searching for it finds it wherever it is.
    assert [d["rustdesk_id"] for d in admin_client.get("/api/v1/devices?search=1002").json()["items"]] == [
        "1002"
    ]
    stats = admin_client.get("/api/v1/admin/dashboard").json()
    assert (stats["total_devices"], stats["archived_devices"]) == (1, 1)
    logs = admin_client.get("/api/v1/admin/audit-logs").json()["items"]
    assert any(i["action"] == "devices_archived" and i["detail"]["count"] == 1 for i in logs)


def test_an_archived_device_returns_by_itself_when_it_reports(admin_client, monkeypatch):
    settings = _with_stale_days(monkeypatch, 30)
    device = _register(admin_client)
    _age("1001", days=45)
    _archive_stale(settings)
    assert admin_client.get(f"/api/v1/devices/{device['id']}").json()["archived"] is True

    _beat(admin_client)

    assert admin_client.get(f"/api/v1/devices/{device['id']}").json()["archived"] is False
    assert [d["rustdesk_id"] for d in admin_client.get("/api/v1/devices").json()["items"]] == ["1001"]
    timeline = admin_client.get(f"/api/v1/devices/{device['id']}/timeline").json()
    assert "unarchived" in [e["kind"] for e in timeline]


def test_a_watched_device_is_never_archived(admin_client, monkeypatch):
    settings = _with_stale_days(monkeypatch, 30)
    device = _register(admin_client)
    _watch(admin_client, device)
    _age("1001", days=90)
    assert _archive_stale(settings) == 0


def test_archiving_is_off_unless_configured(admin_client):
    _register(admin_client)
    _age("1001", days=900)
    assert _archive_stale(get_settings()) == 0


def test_a_device_that_never_reported_is_measured_from_its_creation(admin_client, monkeypatch):
    settings = _with_stale_days(monkeypatch, 30)
    _register(admin_client)
    with get_session_factory()() as db:
        device = db.query(Device).one()
        device.last_seen = None
        device.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60)
        db.commit()
    assert _archive_stale(settings) == 1


def test_a_device_can_be_archived_and_restored_by_hand_and_in_bulk(admin_client):
    device = _register(admin_client)
    r = admin_client.put(f"/api/v1/devices/{device['id']}/archive", json={"archived": True})
    assert r.json() == {"archived": True}
    assert admin_client.get("/api/v1/devices").json()["total"] == 0

    admin_client.post("/api/v1/devices/bulk", json={"ids": [device["id"]], "action": "unarchive"})
    assert admin_client.get("/api/v1/devices").json()["total"] == 1
    admin_client.post("/api/v1/devices/bulk", json={"ids": [device["id"]], "action": "archive"})
    assert admin_client.get("/api/v1/devices?status=archived").json()["total"] == 1


def test_a_user_cannot_archive_a_device_they_cannot_see(app, admin_client):
    device = _register(admin_client)
    admin_client.post("/api/v1/users", json={"username": "bob", "password": "userpassword1"})
    bob = TestClient(app)
    bob.post("/api/v1/auth/login", json={"username": "bob", "password": "userpassword1"})
    bob.headers.update({"X-CSRF-Token": bob.cookies.get("rd_csrf")})
    assert bob.put(f"/api/v1/devices/{device['id']}/archive", json={"archived": True}).status_code == 404


# --- outdated clients and the new-device count -----------------------------------------


def test_clients_older_than_the_minimum_version_are_flagged(app, settings, monkeypatch):
    monkeypatch.setenv("MIN_CLIENT_VERSION", "1.4.0")
    clear_settings_cache()
    from rustdesk_api.app import create_app

    with TestClient(create_app(get_settings())) as admin:
        admin.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
        admin.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"})
        admin.headers.update({"X-CSRF-Token": admin.cookies.get("rd_csrf")})
        _register(admin, "1001", version="1.3.9")
        _register(admin, "1002", uuid="other-uuid-2", version="1.4.1")
        _register(admin, "1003", uuid="other-uuid-3")

        flags = {d["rustdesk_id"]: d["outdated"] for d in admin.get("/api/v1/devices").json()["items"]}
        stats = admin.get("/api/v1/admin/dashboard").json()

    assert flags == {"1001": True, "1002": False, "1003": False}
    assert stats["outdated_devices"] == 1


def test_no_client_is_outdated_without_a_minimum(admin_client):
    _register(admin_client, "1001", version="0.1.0")
    assert admin_client.get("/api/v1/devices").json()["items"][0]["outdated"] is False
    assert admin_client.get("/api/v1/admin/dashboard").json()["outdated_devices"] == 0


def test_the_dashboard_counts_devices_registered_in_the_last_day(admin_client):
    _register(admin_client, "1001")
    _register(admin_client, "1002", uuid="other-uuid-2")
    with get_session_factory()() as db:
        old = db.query(Device).filter_by(rustdesk_id="1002").one()
        old.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
        db.commit()
    assert admin_client.get("/api/v1/admin/dashboard").json()["new_devices_24h"] == 1
