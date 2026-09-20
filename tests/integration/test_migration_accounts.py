"""The accounts migration (lockout counters, API-key scope, parked device uuids,
reset links, saved views, device events) on a database that already has rows,
plus retention of the new tables and the unlock-user command."""

from __future__ import annotations

import datetime

from alembic import command
from click.testing import CliRunner
from sqlalchemy import create_engine, text

from rustdesk_api.cli import main
from rustdesk_api.config import clear_settings_cache
from rustdesk_api.db.database import get_session_factory
from rustdesk_api.db.migrations.runner import get_alembic_config, run_migrations
from rustdesk_api.services import password_reset as reset_service
from rustdesk_api.services import retention as retention_service

PREVIOUS = "d5a1c7e9b3f4"
NOW = "2026-09-01 00:00:00"
NEW_TABLES = {"password_reset_tokens", "saved_views", "device_events"}


def test_upgrade_keeps_existing_rows_and_defaults_the_new_columns(settings):
    cfg = get_alembic_config(settings)
    command.upgrade(cfg, PREVIOUS)
    engine = create_engine(settings.database_url)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO users (id, username, password_hash, is_active, is_admin, created_at, updated_at)"
                " VALUES (1, 'alice', 'x', 1, 0, :t, :t)"
            ),
            {"t": NOW},
        )
        c.execute(
            text(
                "INSERT INTO devices (id, rustdesk_id, owner_id, created_at, updated_at)"
                " VALUES (1, '111', 1, :t, :t)"
            ),
            {"t": NOW},
        )
        c.execute(
            text(
                "INSERT INTO auth_sessions (id, user_id, token_hash, kind, created_at, expires_at)"
                " VALUES (1, 1, 'h', 'webui', :t, :t)"
            ),
            {"t": NOW},
        )

    command.upgrade(cfg, "head")
    with engine.connect() as c:
        user = c.execute(text("SELECT failed_logins, locked_until FROM users")).one()
        assert tuple(user) == (0, None)
        device = c.execute(text("SELECT pending_uuid, pending_uuid_at, pending_uuid_ip FROM devices")).one()
        assert tuple(device) == (None, None, None)
        assert c.execute(text("SELECT scope FROM auth_sessions")).scalar_one() == "full"
        for table in NEW_TABLES:
            assert c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0

    command.downgrade(cfg, PREVIOUS)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM devices")).scalar_one() == 1
        tables = {row[0] for row in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        assert not NEW_TABLES & tables
        columns = {row[1] for row in c.execute(text("PRAGMA table_info(users)"))}
        assert not {"failed_logins", "locked_until"} & columns


def test_retention_drops_old_device_events_and_spent_reset_links(settings):
    run_migrations(settings)
    now = datetime.datetime.now(datetime.timezone.utc)
    old = (now - datetime.timedelta(days=400)).isoformat(sep=" ")
    with get_session_factory()() as db:
        db.execute(
            text(
                "INSERT INTO users (id, username, password_hash, is_active, is_admin, created_at, updated_at)"
                " VALUES (1, 'alice', 'x', 1, 0, :t, :t)"
            ),
            {"t": old},
        )
        db.execute(
            text("INSERT INTO devices (id, rustdesk_id, created_at, updated_at) VALUES (1, '111', :t, :t)"),
            {"t": old},
        )
        db.execute(
            text(
                "INSERT INTO device_events (device_id, kind, created_at)"
                " VALUES (1, 'online', :t), (1, 'online', :n)"
            ),
            {"t": old, "n": now.isoformat(sep=" ")},
        )
        db.execute(
            text(
                "INSERT INTO password_reset_tokens (user_id, token_hash, created_at, expires_at)"
                " VALUES (1, 'h', :t, :t)"
            ),
            {"t": old},
        )
        db.commit()
        retention_service.purge_expired(db, settings)
        assert db.execute(text("SELECT count(*) FROM device_events")).scalar_one() == 1
        assert db.execute(text("SELECT count(*) FROM password_reset_tokens")).scalar_one() == 0
        assert reset_service.purge_expired(db) == 0


def test_unlock_user_command_clears_a_lockout(settings, monkeypatch):
    monkeypatch.setenv("LOGIN_LOCKOUT_THRESHOLD", "1")
    clear_settings_cache()
    runner = CliRunner()
    from fastapi.testclient import TestClient

    from rustdesk_api.app import create_app

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
        client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong-password"})
        assert (
            client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"}
            ).status_code
            == 429
        )
    assert "(locked)" in runner.invoke(main, ["list-users"]).output
    result = runner.invoke(main, ["unlock-user", "--username", "admin"])
    assert result.exit_code == 0 and "unlocked" in result.output
    assert "(locked)" not in runner.invoke(main, ["list-users"]).output
    assert runner.invoke(main, ["unlock-user", "--username", "ghost"]).exit_code != 0
