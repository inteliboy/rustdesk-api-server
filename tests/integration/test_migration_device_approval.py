"""The device-approval migration on a database that already has devices."""

from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, text

from rustdesk_api.db.migrations.runner import get_alembic_config

PREVIOUS = "d9f3a1b7c5e8"
NOW = "2026-09-01 00:00:00"


def test_devices_that_exist_stay_approved(settings):
    cfg = get_alembic_config(settings)
    command.upgrade(cfg, PREVIOUS)
    engine = create_engine(settings.database_url)
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO devices (id, rustdesk_id, created_at, updated_at) VALUES (1, '111', :t, :t)"),
            {"t": NOW},
        )

    command.upgrade(cfg, "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT approval FROM devices")).scalar_one() == "approved"

    command.downgrade(cfg, PREVIOUS)
    with engine.connect() as c:
        assert "approval" not in [r[1] for r in c.execute(text("PRAGMA table_info(devices)"))]
