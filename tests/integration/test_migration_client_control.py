"""The client-control migration (strategies, live connections, notes, 2FA
columns, enrollment token labels) on a database that already has rows."""

from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, text

from rustdesk_api.db.migrations.runner import get_alembic_config

PREVIOUS = "c4d8f1a2e6b3"
NOW = "2026-09-01 00:00:00"


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
        user = c.execute(text("SELECT totp_enabled, totp_secret_enc, totp_last_step FROM users")).one()
        assert (bool(user[0]), user[1], user[2]) == (False, None, None)
        device = c.execute(
            text("SELECT note, live_connections, pending_disconnect, strategy_id FROM devices")
        ).one()
        assert tuple(device) == (None, None, None, None)
        assert c.execute(text("SELECT label FROM auth_sessions")).scalar_one() is None
        for table in ("strategies", "login_challenges", "recovery_codes"):
            assert c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0

    command.downgrade(cfg, PREVIOUS)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM devices")).scalar_one() == 1
        tables = {row[0] for row in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        assert not {"strategies", "login_challenges", "recovery_codes"} & tables
