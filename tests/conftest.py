from __future__ import annotations

import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from rustdesk_api.app import create_app
from rustdesk_api.config import Settings, clear_settings_cache, get_settings
from rustdesk_api.db.database import init_engine
from rustdesk_api.db.migrations.runner import run_migrations
from rustdesk_api.security import passwords


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch) -> None:
    """Nearly every test creates an account and signs in, and Argon2's production
    settings (64 MiB, 3 passes) make that the slowest part of a test on a small CI
    runner. Same algorithm, minimal cost; a hash carries its own parameters, so
    verifying is unaffected. tests/unit/test_passwords.py still checks the format."""
    monkeypatch.setattr(passwords, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


@pytest.fixture()
def settings(tmp_path, monkeypatch) -> Iterator[Settings]:
    """Each test gets its own throwaway SQLite database file, so tests never
    depend on (or pollute) a developer's local database."""
    db_path = tmp_path / f"test-{uuid.uuid4().hex}.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("SESSION_LIFETIME_SECONDS", "3600")
    # No scheduled backups running behind a test's back (the tests that want one call the service).
    monkeypatch.setenv("BACKUP_INTERVAL_HOURS", "0")
    clear_settings_cache()
    yield get_settings()
    clear_settings_cache()


@pytest.fixture(scope="session")
def migrated_database(tmp_path_factory) -> Path:
    """A database with every migration applied, built once per test process.
    Migrating a fresh file for each test (12 revisions, several of which copy whole
    tables on SQLite) was most of a test's time on a Windows runner; a copy of this
    one is already at `head`, so the migration runs the app does at startup find
    nothing to do."""
    path = tmp_path_factory.mktemp("template") / "migrated.db"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", f"sqlite:///{path.as_posix()}")
        mp.setenv("SECRET_KEY", "test-secret-key")
        clear_settings_cache()
        run_migrations(get_settings())
        clear_settings_cache()
    # One self-contained file: no write-ahead log to leave behind when it is copied.
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
    finally:
        connection.close()
    return path


@pytest.fixture()
def app(settings: Settings, migrated_database: Path):
    # `settings` alone gives an empty database (the migration tests start from that).
    database = make_url(settings.database_url).database
    assert database is not None
    shutil.copyfile(migrated_database, database)
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
