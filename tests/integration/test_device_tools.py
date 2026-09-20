"""Bulk device changes, the device timeline, saved views and the extra
device-list filters (status, sort)."""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from rustdesk_api.config import clear_settings_cache
from rustdesk_api.db.database import get_session_factory


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("AUTH_RATE_LIMIT_ATTEMPTS", "1000")
    clear_settings_cache()


def _user_client(app, admin_client, name):
    r = admin_client.post("/api/v1/users", json={"username": name, "password": "userpassword1"})
    assert r.status_code == 201, r.text
    c = TestClient(app)
    c.post("/api/v1/auth/login", json={"username": name, "password": "userpassword1"})
    c.headers.update({"X-CSRF-Token": c.cookies.get("rd_csrf")})
    return c, r.json()["id"]


def _register(client, rustdesk_id, hostname=None, uuid=None):
    r = client.post(
        "/api/sysinfo",
        json={
            "id": rustdesk_id,
            "uuid": uuid or f"uuid-{rustdesk_id}",
            "hostname": hostname or f"host-{rustdesk_id}",
            "os": "linux / x",
        },
    )
    assert r.status_code == 200
    items = client.get("/api/v1/devices", params={"page_size": 200}).json()["items"]
    return next(d for d in items if d["rustdesk_id"] == rustdesk_id)


def _bulk(client, ids, action, **extra):
    return client.post("/api/v1/devices/bulk", json={"ids": ids, "action": action, **extra})


def _tag(client, name="site-a"):
    r = client.post("/api/v1/tags", json={"name": name, "color": "#112233"})
    assert r.status_code == 201
    return r.json()["id"]


# --------------------------------------------------------------------------
# Bulk
# --------------------------------------------------------------------------


def test_bulk_tagging_only_touches_devices_the_caller_may_edit(app, admin_client):
    alice, alice_id = _user_client(app, admin_client, "alice")
    bob, bob_id = _user_client(app, admin_client, "bob")
    mine = _register(admin_client, "1001")
    theirs = _register(admin_client, "1002")
    viewed = _register(admin_client, "1003")
    admin_client.patch(f"/api/v1/devices/{mine['id']}", json={"owner_id": alice_id})
    admin_client.patch(f"/api/v1/devices/{theirs['id']}", json={"owner_id": bob_id})
    admin_client.patch(f"/api/v1/devices/{viewed['id']}", json={"owner_id": bob_id})
    admin_client.post(
        f"/api/v1/devices/{viewed['id']}/shares", json={"username": "alice", "permission": "view"}
    )
    tag_id = _tag(alice)

    r = _bulk(alice, [mine["id"], theirs["id"], viewed["id"], 9999], "add_tag", tag_id=tag_id)
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == [mine["id"]]
    reasons = {s["id"]: s["reason"] for s in body["skipped"]}
    # Not visible and not existing look the same; a view-only share is "forbidden".
    assert reasons == {theirs["id"]: "not_found", viewed["id"]: "forbidden", 9999: "not_found"}

    assert [t["name"] for t in admin_client.get(f"/api/v1/devices/{mine['id']}").json()["tags"]] == ["site-a"]
    assert admin_client.get(f"/api/v1/devices/{theirs['id']}").json()["tags"] == []
    assert admin_client.get(f"/api/v1/devices/{viewed['id']}").json()["tags"] == []


def test_bulk_remove_tag_and_repeats_are_harmless(admin_client):
    a, b = _register(admin_client, "1001"), _register(admin_client, "1002")
    tag_id = _tag(admin_client)
    assert _bulk(admin_client, [a["id"], a["id"], b["id"]], "add_tag", tag_id=tag_id).json()["updated"] == [
        a["id"],
        b["id"],
    ]
    assert _bulk(admin_client, [a["id"]], "add_tag", tag_id=tag_id).status_code == 200  # already tagged
    assert len(admin_client.get(f"/api/v1/devices/{a['id']}").json()["tags"]) == 1
    assert _bulk(admin_client, [a["id"], b["id"]], "remove_tag", tag_id=tag_id).json()["updated"] == [
        a["id"],
        b["id"],
    ]
    assert admin_client.get(f"/api/v1/devices/{b['id']}").json()["tags"] == []


def test_bulk_group_uses_the_same_rules_as_a_single_edit(app, admin_client):
    alice, alice_id = _user_client(app, admin_client, "alice")
    bob, _ = _user_client(app, admin_client, "bob")
    device = _register(admin_client, "1001")
    admin_client.patch(f"/api/v1/devices/{device['id']}", json={"owner_id": alice_id})
    mine = alice.post("/api/v1/groups", json={"name": "mine"}).json()
    other = bob.post("/api/v1/groups", json={"name": "bobs"}).json()

    assert _bulk(alice, [device["id"]], "set_group", group_id=other["id"]).status_code == 404
    assert _bulk(alice, [device["id"]], "set_group", group_id=mine["id"]).json()["updated"] == [device["id"]]
    assert admin_client.get(f"/api/v1/devices/{device['id']}").json()["group_id"] == mine["id"]
    assert _bulk(alice, [device["id"]], "set_group", group_id=None).json()["updated"] == [device["id"]]
    assert admin_client.get(f"/api/v1/devices/{device['id']}").json()["group_id"] is None


def test_bulk_owner_and_strategy_are_admin_only(app, admin_client):
    alice, alice_id = _user_client(app, admin_client, "alice")
    device = _register(admin_client, "1001")
    admin_client.patch(f"/api/v1/devices/{device['id']}", json={"owner_id": alice_id})
    strategy = admin_client.post("/api/v1/strategies", json={"name": "s", "options": {}}).json()

    assert _bulk(alice, [device["id"]], "set_owner", owner_id=alice_id).status_code == 403
    assert _bulk(alice, [device["id"]], "set_strategy", strategy_id=strategy["id"]).status_code == 403

    assert _bulk(admin_client, [device["id"]], "set_strategy", strategy_id=strategy["id"]).json()["updated"]
    assert admin_client.get(f"/api/v1/devices/{device['id']}").json()["strategy_id"] == strategy["id"]
    assert _bulk(admin_client, [device["id"]], "set_owner", owner_id=None).json()["updated"]
    assert admin_client.get(f"/api/v1/devices/{device['id']}").json()["owner_id"] is None
    assert _bulk(admin_client, [device["id"]], "set_owner", owner_id=9999).status_code == 404
    assert _bulk(admin_client, [device["id"]], "set_strategy", strategy_id=9999).status_code == 404


def test_bulk_delete_needs_ownership_and_is_audited_per_device(app, admin_client):
    alice, alice_id = _user_client(app, admin_client, "alice")
    mine, theirs, shared = (_register(admin_client, i) for i in ("1001", "1002", "1003"))
    admin_client.patch(f"/api/v1/devices/{mine['id']}", json={"owner_id": alice_id})
    admin_client.post(
        f"/api/v1/devices/{shared['id']}/shares", json={"username": "alice", "permission": "control"}
    )

    body = _bulk(alice, [mine["id"], theirs["id"], shared["id"]], "delete").json()
    assert body["updated"] == [mine["id"]]
    assert {s["reason"] for s in body["skipped"]} == {"not_found", "forbidden"}
    remaining = {d["rustdesk_id"] for d in admin_client.get("/api/v1/devices").json()["items"]}
    assert remaining == {"1002", "1003"}
    audit = admin_client.get("/api/v1/admin/audit-logs").json()["items"]
    assert any(e["action"] == "device_deleted" and e["target_id"] == mine["id"] for e in audit)


def test_bulk_validates_its_request(admin_client):
    device = _register(admin_client, "1001")
    assert _bulk(admin_client, [], "delete").status_code == 422
    assert _bulk(admin_client, list(range(1, 202)), "delete").status_code == 422
    assert _bulk(admin_client, [device["id"]], "add_tag").status_code == 422  # tag_id missing
    assert _bulk(admin_client, [device["id"]], "set_group").status_code == 422  # explicit null needed
    assert _bulk(admin_client, [device["id"]], "explode").status_code == 422
    assert _bulk(admin_client, [device["id"]], "add_tag", tag_id=9999).status_code == 404


def test_bulk_needs_csrf_for_a_cookie_session(app, admin_client):
    alice, _ = _user_client(app, admin_client, "alice")
    del alice.headers["X-CSRF-Token"]
    assert _bulk(alice, [1], "delete").status_code == 403


# --------------------------------------------------------------------------
# Timeline
# --------------------------------------------------------------------------


def test_the_timeline_merges_events_changes_and_connections(admin_client):
    device = _register(admin_client, "1001", uuid="u1")
    admin_client.patch(f"/api/v1/devices/{device['id']}", json={"alias": "Front desk"})
    long_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat(sep=" ")
    with get_session_factory()() as db:
        db.execute(text("UPDATE devices SET last_seen = :t"), {"t": long_ago})
        db.commit()
    assert admin_client.post("/api/heartbeat", json={"id": "1001", "uuid": "u1"}).status_code == 200
    admin_client.post(
        "/api/audit/conn",
        json={
            "id": "1001",
            "uuid": "u1",
            "conn_id": 1,
            "session_id": 5,
            "nonce": "n1",
            "action": "new",
            "ip": "203.0.113.7",
        },
    )
    admin_client.post(
        "/api/audit/conn",
        json={
            "id": "1001",
            "uuid": "u1",
            "conn_id": 1,
            "session_id": 5,
            "nonce": "n2",
            "peer": ["222333444", "Bob"],
            "type": 0,
        },
    )

    entries = admin_client.get(f"/api/v1/devices/{device['id']}/timeline").json()
    kinds = [(e["source"], e["kind"]) for e in entries]
    assert ("event", "registered") in kinds
    assert ("event", "online") in kinds
    assert ("audit", "device_updated") in kinds
    assert ("connection", "connection") in kinds
    assert [e["at"] for e in entries] == sorted((e["at"] for e in entries), reverse=True)
    online = next(e for e in entries if e["kind"] == "online")
    assert "offline_since" in online["detail"]
    updated = next(e for e in entries if e["kind"] == "device_updated")
    assert updated["actor"] == "admin"
    connection = next(e for e in entries if e["source"] == "connection")
    assert connection["detail"]["peer_id"] == "222333444"


def test_the_timeline_pages_backwards_and_respects_the_limit(admin_client):
    device = _register(admin_client, "1001")
    for i in range(4):
        admin_client.patch(f"/api/v1/devices/{device['id']}", json={"alias": f"a{i}"})
    everything = admin_client.get(f"/api/v1/devices/{device['id']}/timeline", params={"limit": 200}).json()
    assert len(everything) >= 5
    first = admin_client.get(f"/api/v1/devices/{device['id']}/timeline", params={"limit": 2}).json()
    assert len(first) == 2
    older = admin_client.get(
        f"/api/v1/devices/{device['id']}/timeline", params={"limit": 200, "before": first[-1]["at"]}
    ).json()
    assert all(e["at"] < first[-1]["at"] for e in older)
    assert len(older) == len(everything) - 2
    assert (
        admin_client.get(f"/api/v1/devices/{device['id']}/timeline", params={"limit": 0}).status_code == 422
    )


def test_a_repeated_heartbeat_does_not_add_online_events(admin_client):
    device = _register(admin_client, "1001", uuid="u1")
    for _ in range(3):
        admin_client.post("/api/heartbeat", json={"id": "1001", "uuid": "u1"})
    kinds = [e["kind"] for e in admin_client.get(f"/api/v1/devices/{device['id']}/timeline").json()]
    assert kinds.count("online") == 0


def test_the_timeline_of_someone_elses_device_is_a_404(app, admin_client):
    alice, _ = _user_client(app, admin_client, "alice")
    device = _register(admin_client, "1001")
    assert alice.get(f"/api/v1/devices/{device['id']}/timeline").status_code == 404
    assert admin_client.get("/api/v1/devices/9999/timeline").status_code == 404


# --------------------------------------------------------------------------
# Saved views and list filters
# --------------------------------------------------------------------------


def test_saved_views_are_per_user_and_sanitised(app, admin_client):
    alice, _ = _user_client(app, admin_client, "alice")
    r = alice.post(
        "/api/v1/views",
        json={
            "name": "Offline Windows",
            "query": {
                "search": " win ",
                "status": "offline",
                "sort": "name",
                "group_id": 3,
                "evil": "x",
                "tag_id": "7",
                "order": "sideways",
            },
        },
    )
    assert r.status_code == 201
    assert r.json()["query"] == {"search": "win", "status": "offline", "sort": "name", "group_id": 3}

    assert [v["name"] for v in alice.get("/api/v1/views").json()] == ["Offline Windows"]
    assert admin_client.get("/api/v1/views").json() == []  # not shared with anyone

    view_id = r.json()["id"]
    assert admin_client.delete(f"/api/v1/views/{view_id}").status_code == 404
    assert alice.delete(f"/api/v1/views/{view_id}").status_code == 204
    assert alice.get("/api/v1/views").json() == []


def test_saving_under_an_existing_name_replaces_it(admin_client):
    admin_client.post("/api/v1/views", json={"name": "Mine", "query": {"status": "online"}})
    admin_client.post("/api/v1/views", json={"name": "mine", "query": {"status": "offline"}})
    views = admin_client.get("/api/v1/views").json()
    assert len(views) == 1 and views[0]["query"] == {"status": "offline"}
    assert admin_client.post("/api/v1/views", json={"name": "  ", "query": {}}).status_code == 422
    assert admin_client.post("/api/v1/views", json={"name": "x", "query": []}).status_code == 422


def test_the_device_list_filters_by_status_and_sorts(admin_client):
    for ident, host in (("1001", "charlie"), ("1002", "alpha"), ("1003", "bravo")):
        _register(admin_client, ident, hostname=host)
    long_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat(sep=" ")
    with get_session_factory()() as db:
        db.execute(text("UPDATE devices SET last_seen = :t WHERE rustdesk_id = '1002'"), {"t": long_ago})
        db.commit()

    def ids(**params):
        return [d["rustdesk_id"] for d in admin_client.get("/api/v1/devices", params=params).json()["items"]]

    assert sorted(ids(status="online")) == ["1001", "1003"]
    assert ids(status="offline") == ["1002"]
    assert ids(sort="name") == ["1002", "1003", "1001"]  # alpha, bravo, charlie
    assert ids(sort="name", order="desc") == ["1001", "1003", "1002"]
    assert ids(sort="id") == ["1001", "1002", "1003"]
    assert admin_client.get("/api/v1/devices", params={"status": "sleeping"}).status_code == 422
    assert admin_client.get("/api/v1/devices", params={"sort": "password"}).status_code == 422
    total = admin_client.get("/api/v1/devices", params={"status": "online"}).json()["total"]
    assert total == 2


def test_a_share_holder_sees_events_and_connections_but_not_who_changed_the_device(app, admin_client):
    alice, alice_id = _user_client(app, admin_client, "alice")
    bob, _ = _user_client(app, admin_client, "bob")
    device = _register(admin_client, "1001", uuid="u1")
    admin_client.patch(f"/api/v1/devices/{device['id']}", json={"owner_id": alice_id, "alias": "Front desk"})
    alice.post(f"/api/v1/devices/{device['id']}/shares", json={"username": "bob", "permission": "view"})

    owner_view = alice.get(f"/api/v1/devices/{device['id']}/timeline").json()
    assert {"device_updated", "device_shared"} <= {e["kind"] for e in owner_view}

    shared_view = bob.get(f"/api/v1/devices/{device['id']}/timeline").json()
    assert {e["source"] for e in shared_view} <= {"event", "connection"}
    assert "registered" in {e["kind"] for e in shared_view}
    assert "device_shared" not in str(shared_view) and "alice" not in str(shared_view)
