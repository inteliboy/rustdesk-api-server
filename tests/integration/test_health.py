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
    assert "version" in r.json()


def test_unknown_route_returns_json_error_envelope(client):
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    assert "error" in r.json()
