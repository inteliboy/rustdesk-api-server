"""Prometheus /metrics, and the WEBUI_ALLOWED_NETWORKS restriction."""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rustdesk_api.cli import main
from rustdesk_api.config import Settings, clear_settings_cache

TOKEN = "scrape-token-0123456789abcdef"


def _app(monkeypatch, **env):
    monkeypatch.setenv("AUTH_RATE_LIMIT_ATTEMPTS", "1000")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    clear_settings_cache()
    from rustdesk_api.app import create_app

    return create_app()


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def test_metrics_are_off_unless_a_token_is_configured(admin_client):
    assert admin_client.get("/metrics").status_code == 404
    assert admin_client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404


def test_metrics_need_the_token(monkeypatch, client):
    app = _app(monkeypatch, METRICS_TOKEN=TOKEN)
    c = TestClient(app)
    assert c.get("/metrics").status_code == 401
    assert c.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/metrics", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    ok = c.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/plain")


def test_metrics_report_the_fleet(monkeypatch, admin_client):
    admin_client.post("/api/sysinfo", json={"id": "1001", "uuid": "u1", "hostname": "a", "os": "linux / x"})
    admin_client.post("/api/sysinfo", json={"id": "1002", "uuid": "u2", "hostname": "b", "os": "linux / x"})
    admin_client.post("/api/heartbeat", json={"id": "1001", "uuid": "u1", "conns": [3, 4]})
    admin_client.post(
        "/api/sysinfo", json={"id": "1001", "uuid": "intruder", "hostname": "x", "os": "linux / x"}
    )

    app = _app(monkeypatch, METRICS_TOKEN=TOKEN)
    body = TestClient(app).get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"}).text
    lines = {line.split(" ")[0]: line.split(" ")[1] for line in body.splitlines() if not line.startswith("#")}
    assert lines["rustdesk_api_devices"] == "2"
    assert lines["rustdesk_api_devices_online"] == "2"
    assert lines["rustdesk_api_devices_offline"] == "0"
    assert lines["rustdesk_api_devices_uuid_change_pending"] == "1"
    assert lines["rustdesk_api_connections_open"] == "2"
    assert lines["rustdesk_api_users"] == "1"
    assert lines['rustdesk_api_logins_24h{result="success"}'] == "1"
    assert lines['rustdesk_api_sessions_active{kind="webui"}'] == "1"
    assert "# TYPE rustdesk_api_heartbeats_total counter" in body
    assert 'rustdesk_api_info{version="' in body
    # No secrets or per-device data: only counts.
    assert "u1" not in body and TOKEN not in body


def test_a_short_metrics_token_is_a_startup_error(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "short")
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    assert "short" not in str(excinfo.value).replace("METRICS_TOKEN", "")


def test_check_config_redacts_the_metrics_token(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", TOKEN)
    clear_settings_cache()
    result = CliRunner().invoke(main, ["check-config"])
    assert result.exit_code == 0
    assert TOKEN not in result.output
    assert "metrics_token=***redacted***" in result.output


# --------------------------------------------------------------------------
# Allowed networks
# --------------------------------------------------------------------------


def _from(client, address, path="/api/v1/auth/setup/status", **kwargs):
    return client.get(path, headers={"X-Forwarded-For": address}, **kwargs)


def test_only_listed_networks_reach_the_webui_and_the_management_api(monkeypatch, client):
    app = _app(monkeypatch, WEBUI_ALLOWED_NETWORKS="10.0.0.0/8, 192.168.1.5", TRUSTED_PROXIES="testclient")
    c = TestClient(app)
    assert _from(c, "10.1.2.3").status_code == 200
    assert _from(c, "192.168.1.5").status_code == 200
    blocked = _from(c, "203.0.113.9")
    assert (blocked.status_code, blocked.json()["error"]["code"]) == (403, "NETWORK_NOT_ALLOWED")
    assert _from(c, "192.168.1.6").status_code == 403

    page = _from(c, "203.0.113.9", "/login")
    assert (page.status_code, page.text) == (403, "Forbidden")
    assert _from(c, "10.9.9.9", "/login").status_code == 200
    assert _from(c, "203.0.113.9", "/docs").status_code == 403


def test_the_rustdesk_client_protocol_and_health_stay_open(monkeypatch, client):
    app = _app(monkeypatch, WEBUI_ALLOWED_NETWORKS="10.0.0.0/8", TRUSTED_PROXIES="testclient")
    c = TestClient(app)
    outside = {"X-Forwarded-For": "203.0.113.9"}
    assert c.get("/health", headers=outside).status_code == 200
    assert c.post("/api/heartbeat", json={"id": "1", "uuid": "u"}, headers=outside).status_code == 200
    assert (
        c.post("/api/sysinfo", json={"id": "1", "uuid": "u", "os": "linux / x"}, headers=outside).status_code
        == 200
    )
    assert c.post("/api/login", json={"username": "x", "password": "y"}, headers=outside).status_code == 200
    assert c.get("/api/login-options", headers=outside).json() == []
    assert c.get("/metrics", headers=outside).status_code == 404  # reachable (and token-gated), not blocked


def test_ipv6_and_ipv4_mapped_addresses(monkeypatch, client):
    app = _app(monkeypatch, WEBUI_ALLOWED_NETWORKS="10.0.0.0/8,2001:db8::/32", TRUSTED_PROXIES="testclient")
    c = TestClient(app)
    assert _from(c, "2001:db8::1").status_code == 200
    assert _from(c, "2001:dead::1").status_code == 403
    assert _from(c, "::ffff:10.1.1.1").status_code == 200
    assert _from(c, "::ffff:8.8.8.8").status_code == 403


def test_an_unknown_or_unparseable_address_is_refused(monkeypatch, client):
    app = _app(monkeypatch, WEBUI_ALLOWED_NETWORKS="10.0.0.0/8", TRUSTED_PROXIES="testclient")
    c = TestClient(app)
    assert _from(c, "not-an-ip").status_code == 403
    # The test client's own "address" is a host name, so without a trusted
    # forwarded header there is nothing to match: fail closed.
    assert c.get("/api/v1/auth/setup/status").status_code == 403


def test_the_websocket_follows_the_same_rule(monkeypatch, client):
    from starlette.websockets import WebSocketDisconnect

    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    app = _app(monkeypatch, WEBUI_ALLOWED_NETWORKS="10.0.0.0/8", TRUSTED_PROXIES="testclient")
    c = TestClient(app)
    login = c.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "adminpass123"},
        headers={"X-Forwarded-For": "10.1.1.1"},
    )
    assert login.status_code == 200

    with pytest.raises(WebSocketDisconnect):  # valid session, wrong network
        with c.websocket_connect("/api/v1/ws/devices", headers={"X-Forwarded-For": "203.0.113.9"}):
            pass
    with c.websocket_connect("/api/v1/ws/devices", headers={"X-Forwarded-For": "10.1.1.1"}):
        pass  # allowed network, valid session: accepted


def test_forwarded_headers_are_ignored_from_an_untrusted_peer(monkeypatch, client):
    app = _app(monkeypatch, WEBUI_ALLOWED_NETWORKS="10.0.0.0/8")  # no TRUSTED_PROXIES
    c = TestClient(app)
    assert _from(c, "10.1.2.3").status_code == 403  # a spoofed header buys nothing


def test_no_restriction_by_default(client):
    assert client.get("/api/v1/auth/setup/status").status_code == 200


def test_a_bad_network_list_is_a_startup_error(monkeypatch):
    monkeypatch.setenv("WEBUI_ALLOWED_NETWORKS", "10.0.0.0/8, banana")
    with pytest.raises(ValidationError):
        Settings()


def test_check_config_mentions_the_restriction(monkeypatch):
    monkeypatch.setenv("WEBUI_ALLOWED_NETWORKS", "10.0.0.0/8")
    clear_settings_cache()
    result = CliRunner().invoke(main, ["check-config"])
    assert "answer only to 10.0.0.0/8" in result.output
