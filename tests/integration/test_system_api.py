"""The administrator's view of notifications and backups, and the client setup helper."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rustdesk_api.config import clear_settings_cache
from rustdesk_api.services import client_config, notifications


def _ordinary_user(app, admin_client, name="bob"):
    r = admin_client.post("/api/v1/users", json={"username": name, "password": "userpassword1"})
    assert r.status_code == 201, r.text
    other = TestClient(app)
    other.post("/api/v1/auth/login", json={"username": name, "password": "userpassword1"})
    other.headers.update({"X-CSRF-Token": other.cookies.get("rd_csrf")})
    return other


# --- overview -------------------------------------------------------------


def test_the_overview_reports_what_is_configured_without_any_secret(app, settings, monkeypatch):
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks.example.com/services/T0/B0/secret-token")
    monkeypatch.setenv("NOTIFY_WEBHOOK_SECRET", "signing-secret-value")
    monkeypatch.setenv("DEVICE_STALE_DAYS", "45")
    monkeypatch.setenv("MIN_CLIENT_VERSION", "1.4.0")
    clear_settings_cache()
    from rustdesk_api.app import create_app
    from rustdesk_api.config import get_settings

    with TestClient(create_app(get_settings())) as admin:
        admin.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
        admin.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"})
        r = admin.get("/api/v1/admin/system")

    assert r.status_code == 200, r.text
    body = r.json()
    channels = {c["name"]: c for c in body["notifications"]["channels"]}
    assert channels["webhook"] == {"name": "webhook", "configured": True, "target": "hooks.example.com"}
    assert channels["ntfy"]["configured"] is False and channels["email"]["configured"] is False
    events = {e["name"]: e["enabled"] for e in body["notifications"]["events"]}
    assert events["new_device"] is True and events["device_back_online"] is False
    assert body["fleet"] == {"stale_days": 45, "min_client_version": "1.4.0"}
    assert body["backups"]["supported"] is True and body["backups"]["items"] == []
    # Nothing that could be a credential is anywhere in the answer.
    assert "secret-token" not in r.text and "signing-secret-value" not in r.text


def test_the_overview_is_for_administrators_only(app, admin_client):
    bob = _ordinary_user(app, admin_client)
    assert bob.get("/api/v1/admin/system").status_code == 403
    assert TestClient(app).get("/api/v1/admin/system").status_code == 401


# --- test notification ----------------------------------------------------


def test_a_test_notification_needs_a_configured_channel(admin_client):
    r = admin_client.post("/api/v1/admin/notifications/test")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NO_NOTIFICATION_CHANNEL"


def test_a_test_notification_reports_each_channel_and_is_audited(admin_client, monkeypatch):
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks.example.com/x")
    monkeypatch.setenv("NOTIFY_NTFY_URL", "https://ntfy.example.com/topic")
    clear_settings_cache()
    sent = []

    def fake_deliver(settings, note, **_kwargs):
        sent.append(note)
        return {"webhook": None, "ntfy": "the server answered HTTP 403"}

    monkeypatch.setattr(notifications, "deliver", fake_deliver)

    r = admin_client.post("/api/v1/admin/notifications/test")

    assert r.status_code == 200, r.text
    assert r.json() == [
        {"channel": "webhook", "ok": True, "error": None},
        {"channel": "ntfy", "ok": False, "error": "the server answered HTTP 403"},
    ]
    assert sent[0].event == "test" and "admin" in sent[0].message
    logs = admin_client.get("/api/v1/admin/audit-logs").json()["items"]
    entry = next(i for i in logs if i["action"] == "notification_test")
    assert entry["result"] == "failure"


def test_only_an_administrator_can_send_a_test_notification(app, admin_client):
    bob = _ordinary_user(app, admin_client)
    assert bob.post("/api/v1/admin/notifications/test").status_code == 403


# --- backups --------------------------------------------------------------


def test_an_administrator_can_take_and_list_a_backup(admin_client):
    r = admin_client.post("/api/v1/admin/backups")
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["kind"] == "manual" and created["size"] > 0

    listing = admin_client.get("/api/v1/admin/system").json()["backups"]
    assert [b["name"] for b in listing["items"]] == [created["name"]]
    assert listing["directory"].endswith("backups")

    logs = admin_client.get("/api/v1/admin/audit-logs").json()["items"]
    assert any(i["action"] == "backup_created" and i["detail"]["name"] == created["name"] for i in logs)


def test_only_an_administrator_can_take_a_backup(app, admin_client):
    bob = _ordinary_user(app, admin_client)
    assert bob.post("/api/v1/admin/backups").status_code == 403
    assert admin_client.get("/api/v1/admin/system").json()["backups"]["items"] == []


def test_a_backup_needs_the_csrf_token(app, admin_client):
    without = TestClient(app)
    without.cookies.update(admin_client.cookies)
    assert without.post("/api/v1/admin/backups").status_code == 403


# --- client setup helper ---------------------------------------------------


def test_the_defaults_come_from_the_server_settings(app, settings, monkeypatch):
    monkeypatch.setenv("RUSTDESK_ID_SERVER", "id.example.com")
    monkeypatch.setenv("RUSTDESK_RELAY_SERVER", "relay.example.com")
    monkeypatch.setenv("RUSTDESK_KEY", "PublicKeyValue=")
    monkeypatch.setenv("EXTERNAL_URL", "https://rustdesk.example.com/")
    clear_settings_cache()
    from rustdesk_api.app import create_app
    from rustdesk_api.config import get_settings

    with TestClient(create_app(get_settings())) as admin:
        admin.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
        admin.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"})
        r = admin.get("/api/v1/connect")

    assert r.json() == {
        "id_server": "id.example.com",
        "relay_server": "relay.example.com",
        "api_server": "https://rustdesk.example.com",
        "key": "PublicKeyValue=",
    }


def test_the_config_string_a_user_gets_decodes_to_what_they_entered(admin_client):
    r = admin_client.post(
        "/api/v1/connect/config",
        json={
            "id_server": " id.example.com ",
            "relay_server": "",
            "api_server": "https://rustdesk.example.com/",
            "key": "abcKEY",
        },
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert client_config.decode_config_string(body["config_string"]) == {
        "host": "id.example.com",
        "relay": "",
        "api": "https://rustdesk.example.com",
        "key": "abcKEY",
    }
    assert body["exe_name"] == "rustdesk-host=id.example.com,key=abcKEY,api=https://rustdesk.example.com,.exe"
    assert body["qr_svg"].startswith("data:image/svg+xml")
    assert body["warnings"] == []


def test_settings_that_the_client_would_mishandle_are_flagged(admin_client):
    r = admin_client.post(
        "/api/v1/connect/config",
        json={"id_server": "id.example.com", "api_server": "https://rustdesk.example.com:21114"},
    )
    assert set(r.json()["warnings"]) == {"https_21114", "no_key"}


def test_the_setup_helper_needs_a_signed_in_user_and_an_id_server(app, admin_client):
    anonymous = TestClient(app)
    assert anonymous.get("/api/v1/connect").status_code == 401
    assert anonymous.post("/api/v1/connect/config", json={"id_server": "x"}).status_code in (401, 403)
    assert admin_client.post("/api/v1/connect/config", json={"id_server": ""}).status_code == 422


def test_an_ordinary_user_can_use_the_setup_helper(app, admin_client):
    bob = _ordinary_user(app, admin_client)
    assert bob.get("/api/v1/connect").status_code == 200
    assert bob.post("/api/v1/connect/config", json={"id_server": "id.example.com"}).status_code == 200


# --- options set with environment variables -------------------------------


def test_the_options_report_says_what_is_on_and_shows_no_secret(app, settings, monkeypatch):
    monkeypatch.setenv("ALLOW_REGISTRATION", "true")
    monkeypatch.setenv("METRICS_TOKEN", "metrics-token-value-1234")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks.example.com/services/T0/B0/secret-token")
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.1")
    clear_settings_cache()
    from rustdesk_api.app import create_app
    from rustdesk_api.config import get_settings

    with TestClient(create_app(get_settings())) as admin:
        admin.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
        admin.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"})
        r = admin.get("/api/v1/admin/options")

    assert r.status_code == 200, r.text
    groups = {g["name"]: g["items"] for g in r.json()}
    assert list(groups)[0] == "Sign-in and accounts"
    items = {i["env"][0]: i for g in groups.values() for i in g}
    assert items["ALLOW_REGISTRATION"]["enabled"] is True
    assert items["METRICS_TOKEN"]["enabled"] is True and items["METRICS_TOKEN"]["detail"] is None
    assert items["TRUSTED_PROXIES"]["args"] == [1]
    assert items["NOTIFY_WEBHOOK_URL"]["args"] == ["hooks.example.com"]
    assert items["NOTIFY_NTFY_URL"]["enabled"] is False
    assert "secret-token" not in r.text and "metrics-token-value" not in r.text


def test_the_options_report_is_for_administrators_only(app, admin_client):
    bob = _ordinary_user(app, admin_client)
    assert bob.get("/api/v1/admin/options").status_code == 403
    assert TestClient(app).get("/api/v1/admin/options").status_code == 401
