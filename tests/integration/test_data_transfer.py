"""CSV/JSON export of devices, users and the audit log, and import of device
management data."""

from __future__ import annotations

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from rustdesk_api.config import clear_settings_cache


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


def _register(client, rustdesk_id, hostname="host", uuid=None):
    client.post(
        "/api/sysinfo",
        json={
            "id": rustdesk_id,
            "uuid": uuid or f"secret-uuid-{rustdesk_id}",
            "hostname": hostname,
            "os": "linux / x",
        },
    )
    items = client.get("/api/v1/devices", params={"page_size": 200}).json()["items"]
    return next(d for d in items if d["rustdesk_id"] == rustdesk_id)


def _rows(response):
    return list(csv.DictReader(io.StringIO(response.text)))


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def test_device_export_is_csv_with_no_secrets(admin_client):
    device = _register(admin_client, "1001", hostname="front-desk")
    admin_client.patch(f"/api/v1/devices/{device['id']}", json={"alias": "Reception", "note": "top floor"})

    r = admin_client.get("/api/v1/export/devices")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="devices.csv"' in r.headers["content-disposition"]
    rows = _rows(r)
    assert rows[0]["rustdesk_id"] == "1001" and rows[0]["alias"] == "Reception"
    assert rows[0]["note"] == "top floor" and rows[0]["online"] == "True"
    assert "secret-uuid" not in r.text and "uuid" not in r.text.splitlines()[0]


def test_export_neutralises_spreadsheet_formulas(admin_client):
    device = _register(admin_client, "1001")
    admin_client.patch(
        f"/api/v1/devices/{device['id']}", json={"alias": '=HYPERLINK("http://evil")', "note": "-2+3"}
    )
    row = _rows(admin_client.get("/api/v1/export/devices"))[0]
    assert row["alias"].startswith("'=") and row["note"].startswith("'-")


def test_device_export_json(admin_client):
    _register(admin_client, "1001")
    r = admin_client.get("/api/v1/export/devices", params={"format": "json"})
    assert r.headers["content-type"].startswith("application/json")
    assert json.loads(r.text)[0]["rustdesk_id"] == "1001"
    assert admin_client.get("/api/v1/export/devices", params={"format": "xml"}).status_code == 422


def test_a_user_exports_only_what_they_can_see(app, admin_client):
    alice, alice_id = _user_client(app, admin_client, "alice")
    mine, theirs = _register(admin_client, "1001"), _register(admin_client, "1002")
    admin_client.patch(f"/api/v1/devices/{mine['id']}", json={"owner_id": alice_id})
    ids = [row["rustdesk_id"] for row in _rows(alice.get("/api/v1/export/devices"))]
    assert ids == ["1001"]
    assert theirs["rustdesk_id"] not in alice.get("/api/v1/export/devices").text


def test_user_and_audit_exports_are_admin_only_and_hold_no_secrets(app, admin_client):
    alice, _ = _user_client(app, admin_client, "alice")
    for path in ("/api/v1/export/users", "/api/v1/export/audit"):
        assert alice.get(path).status_code == 403
        assert TestClient(app).get(path).status_code == 401

    users = _rows(admin_client.get("/api/v1/export/users"))
    assert {u["username"] for u in users} == {"admin", "alice"}
    assert "hash" not in admin_client.get("/api/v1/export/users").text.lower()

    audit = admin_client.get("/api/v1/export/audit", params={"days": 7})
    assert audit.status_code == 200
    assert {"at", "actor", "action", "result"} <= set(_rows(audit)[0])
    assert admin_client.get("/api/v1/export/audit", params={"days": 0}).status_code == 422


def test_exporting_is_itself_audited(admin_client):
    admin_client.get("/api/v1/export/devices")
    audit = admin_client.get("/api/v1/admin/audit-logs").json()["items"]
    assert any(e["action"] == "data_exported" for e in audit)


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

CSV = "text/csv"


def _import(client, body, content_type=CSV, **params):
    data = body if isinstance(body, (bytes, str)) else json.dumps(body)
    return client.post(
        "/api/v1/import/devices", content=data, headers={"Content-Type": content_type}, params=params
    )


def _find(client, rustdesk_id):
    items = client.get("/api/v1/devices", params={"page_size": 200}).json()["items"]
    return next((d for d in items if d["rustdesk_id"] == rustdesk_id), None)


def test_import_creates_and_updates_devices(app, admin_client):
    _user_client(app, admin_client, "alice")
    group = admin_client.post("/api/v1/groups", json={"name": "Reception"}).json()
    existing = _register(admin_client, "1001", hostname="kept")
    body = (
        "rustdesk_id,alias,note,owner,group,tags\r\n"
        "1001,Front desk,ground floor,alice,Reception,site-a;vip\r\n"
        "2002,New machine,,alice,,site-a\r\n"
    )
    r = _import(admin_client, body)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "dry_run": False,
        "applied": True,
        "created": 1,
        "updated": 1,
        "tags_created": 2,
        "errors": [],
    }

    updated = _find(admin_client, "1001")
    assert updated["id"] == existing["id"] and updated["hostname"] == "kept"
    assert (updated["alias"], updated["note"], updated["owner_username"]) == (
        "Front desk",
        "ground floor",
        "alice",
    )
    assert updated["group_id"] == group["id"]
    assert sorted(t["name"] for t in updated["tags"]) == ["site-a", "vip"]
    created = _find(admin_client, "2002")
    assert (created["alias"], created["owner_username"], created["online"]) == ("New machine", "alice", False)
    assert [t["name"] for t in created["tags"]] == ["site-a"]
    audit = admin_client.get("/api/v1/admin/audit-logs").json()["items"]
    assert any(e["action"] == "devices_imported" for e in audit)


def test_an_imported_device_is_adopted_by_the_real_client_later(admin_client):
    assert _import(admin_client, "rustdesk_id,alias\n3003,Pre-registered\n").json()["applied"] is True
    _register(admin_client, "3003", hostname="real", uuid="first-uuid")
    device = _find(admin_client, "3003")
    assert (device["alias"], device["hostname"], device["uuid_change_pending"]) == (
        "Pre-registered",
        "real",
        False,
    )
    assert len(admin_client.get("/api/v1/devices").json()["items"]) == 1


def test_an_empty_cell_leaves_the_field_alone(admin_client):
    device = _register(admin_client, "1001")
    admin_client.patch(f"/api/v1/devices/{device['id']}", json={"alias": "Keep me", "note": "keep"})
    _import(admin_client, "rustdesk_id,alias,note\n1001,,new note\n")
    now = _find(admin_client, "1001")
    assert (now["alias"], now["note"]) == ("Keep me", "new note")


def test_one_bad_row_applies_nothing_and_each_problem_is_reported(admin_client):
    _register(admin_client, "1001")
    body = (
        "rustdesk_id,alias,owner,group\n"
        "1001,Good,,\n"
        ",No id,,\n"
        "1001,Duplicate,,\n"
        "4004,Bad owner,nobody,\n"
        "5005,Bad group,,Nowhere\n"
    )
    r = _import(admin_client, body)
    assert r.status_code == 200
    result = r.json()
    assert result["applied"] is False
    assert [e["row"] for e in result["errors"]] == [2, 3, 4, 5]
    assert "appears more than once" in result["errors"][1]["message"]
    assert _find(admin_client, "1001")["alias"] is None
    assert _find(admin_client, "4004") is None


def test_dry_run_reports_without_changing_anything(admin_client):
    r = _import(admin_client, "rustdesk_id,alias,tags\n1001,x,new-tag\n", dry_run="true")
    assert r.json() | {"errors": []} == {
        "dry_run": True,
        "applied": False,
        "created": 1,
        "updated": 0,
        "tags_created": 1,
        "errors": [],
    }
    assert _find(admin_client, "1001") is None
    assert admin_client.get("/api/v1/tags").json() == []


def test_json_import_and_header_aliases(admin_client):
    r = _import(
        admin_client, {"rows": [{"ID": "1001", "Alias": "From JSON", "tags": ["a", "b"]}]}, "application/json"
    )
    assert r.json()["applied"] is True
    assert sorted(t["name"] for t in _find(admin_client, "1001")["tags"]) == ["a", "b"]
    assert _import(admin_client, [{"rustdesk_id": "1002"}], "application/json").json()["created"] == 1


def test_import_is_admin_only_and_needs_csrf(app, admin_client):
    alice, _ = _user_client(app, admin_client, "alice")
    assert _import(alice, "rustdesk_id\n1\n").status_code == 403
    assert _import(TestClient(app), "rustdesk_id\n1\n").status_code == 401
    del admin_client.headers["X-CSRF-Token"]
    assert _import(admin_client, "rustdesk_id\n1\n").status_code == 403


def test_import_rejects_unusable_files(admin_client):
    assert _import(admin_client, "").status_code == 422
    assert _import(admin_client, "rustdesk_id\n").status_code == 422  # header only
    assert _import(admin_client, b"\xff\xfe\x00bad", CSV).status_code == 422
    assert _import(admin_client, "{not json", "application/json").status_code == 422
    assert _import(admin_client, {"rows": "nope"}, "application/json").status_code == 422
    too_many = "rustdesk_id\n" + "\n".join(str(i) for i in range(5001)) + "\n"
    assert _import(admin_client, too_many).status_code == 422
    assert _import(admin_client, "x" * (2 * 1024 * 1024 + 1)).status_code == 413
