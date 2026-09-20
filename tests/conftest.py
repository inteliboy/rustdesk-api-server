from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from rustdesk_api.app import create_app
from rustdesk_api.config import Settings, clear_settings_cache, get_settings
from rustdesk_api.db.database import init_engine
from rustdesk_api.db.migrations.runner import run_migrations


@pytest.fixture()
def settings(tmp_path, monkeypatch) -> Iterator[Settings]:
    """Each test gets its own throwaway SQLite database file, so tests never
    depend on (or pollute) a developer's local database."""
    db_path = tmp_path / f"test-{uuid.uuid4().hex}.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("SESSION_LIFETIME_SECONDS", "3600")
    clear_settings_cache()
    yield get_settings()
    clear_settings_cache()


@pytest.fixture()
def app(settings: Settings):
    init_engine(settings)
    run_migrations(settings)
    return create_app(settings)


@pytest.fixture()
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin_client(client: TestClient) -> TestClient:
    """A client that has completed first-run setup and is logged in as the
    resulting administrator, with CSRF header pre-attached."""
    client.post(
        "/api/v1/auth/setup",
        json={"username": "admin", "password": "adminpass123", "email": "admin@example.com"},
    )
    client.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"})
    csrf = client.cookies.get("rd_csrf")
    client.headers.update({"X-CSRF-Token": csrf})
    return client
