"""The admin Server tab: hardware/software facts and the CPU/memory history."""

import time

from rustdesk_api.services.server_metrics import MetricsSampler


def _as_user(client, username="alice", password="alicepassword1"):
    client.post("/api/v1/users", json={"username": username, "password": password, "is_admin": False})
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login", json={"username": username, "password": password})


def test_server_status_requires_an_administrator(admin_client):
    _as_user(admin_client)
    assert admin_client.get("/api/v1/admin/server").status_code == 403

    admin_client.cookies.clear()
    assert admin_client.get("/api/v1/admin/server").status_code == 401


def test_server_status_describes_the_host(admin_client):
    body = admin_client.get("/api/v1/admin/server").json()

    hardware = body["hardware"]
    assert hardware["cpu_model"]
    assert hardware["cpu_logical_cores"] >= 1
    assert hardware["memory_total"] > 0
    assert hardware["data_disk"]["total"] >= hardware["data_disk"]["free"] > 0

    assert body["system"]["os"]
    assert body["software"]["python"]
    build = body["software"]["build"]
    assert build["version"] == body["software"]["app_version"]
    assert build["rustdesk_client_source"]
    assert {"commit", "commit_short", "commit_url", "dirty"} <= build.keys()
    assert body["software"]["packages"]["fastapi"]
    assert body["database"]["engine"] == "sqlite"
    assert body["database"]["size_bytes"] > 0
    assert body["process"]["uptime_seconds"] >= 0
    assert body["interval_seconds"] == 10
    assert body["history_minutes"] == 60


def test_server_status_does_not_expose_configuration_secrets(admin_client, settings, tmp_path):
    text = admin_client.get("/api/v1/admin/server").text
    assert settings.secret_key not in text
    assert settings.database_url not in text
    assert str(tmp_path) not in text  # no filesystem paths from the configuration


def test_samples_are_served_oldest_first_and_only_the_new_ones_after_since(admin_client):
    sampler = admin_client.app.state.metrics_sampler
    first = sampler.sample()
    time.sleep(0.01)  # timestamps are rounded to the millisecond; a fast machine could repeat one
    second = sampler.sample()

    everything = admin_client.get("/api/v1/admin/server").json()["samples"]
    assert [s["t"] for s in everything][-2:] == [first.t, second.t]
    assert everything == sorted(everything, key=lambda s: s["t"])

    newer = admin_client.get("/api/v1/admin/server", params={"since": first.t}).json()["samples"]
    assert [s["t"] for s in newer] == [second.t]
    assert admin_client.get("/api/v1/admin/server", params={"since": second.t}).json()["samples"] == []


def test_a_sample_is_in_range(admin_client):
    sample = admin_client.app.state.metrics_sampler.sample()
    assert 0 <= sample.process_cpu <= 100
    assert 0 <= sample.system_cpu <= 100
    assert 0 < sample.system_memory_percent <= 100
    assert sample.process_memory > 0
    assert sample.threads >= 1


def test_the_history_window_is_bounded():
    sampler = MetricsSampler(interval_seconds=5, history_minutes=5)  # 60 readings
    for _ in range(75):
        sampler.sample()
    assert len(sampler.samples()) == 60


def test_the_dashboard_page_loads_the_server_panel(admin_client):
    r = admin_client.get("/dashboard")
    assert r.status_code == 200
    assert "/static/js/pages/dashboard_server.js" in r.text
    assert 'id="server-section"' in r.text
    assert admin_client.get("/server").status_code == 404  # no separate tab
