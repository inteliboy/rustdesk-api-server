def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ready_endpoint_reports_database_ok(client):
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok"}


def test_version_endpoint(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body
    assert body["rustdesk_client_source"]
    assert body["repository_url"].startswith("https://github.com/")
    # commit fields are always present; null when the build cannot say
    assert {"commit", "commit_short", "commit_url", "dirty"} <= body.keys()


def test_unknown_route_returns_json_error_envelope(client):
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    assert "error" in r.json()
