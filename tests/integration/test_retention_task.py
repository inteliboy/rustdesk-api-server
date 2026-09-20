"""The retention task and CLI command, as an operator meets them."""

import asyncio
import datetime
import json

from click.testing import CliRunner
from sqlalchemy import select

from rustdesk_api import maintenance
from rustdesk_api.cli import main
from rustdesk_api.config import clear_settings_cache, get_settings
from rustdesk_api.db.database import get_session_factory
from rustdesk_api.models.audit import AuditLog
from rustdesk_api.models.client_audit import ConnectionLog


def _old(days: int) -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)


def _seed_old_rows() -> None:
    with get_session_factory()() as db:
        db.add(AuditLog(action="ancient", created_at=_old(999)))
        db.add(ConnectionLog(rustdesk_id="1", started_at=_old(999)))
        db.commit()


def test_startup_purges_expired_logs_in_the_background(app, settings):
    """Given expired logs, when the server starts, then they are removed
    without anyone calling anything."""
    from fastapi.testclient import TestClient

    _seed_old_rows()
    with TestClient(app):
        # The task runs in a worker thread right after startup.
        for _ in range(100):
            with get_session_factory()() as db:
                if db.execute(select(ConnectionLog)).first() is None:
                    break
            asyncio.run(asyncio.sleep(0.05))
        with get_session_factory()() as db:
            assert db.execute(select(ConnectionLog)).first() is None
            assert {a.action for a in db.execute(select(AuditLog)).scalars()} == {"log_retention_purge"}


def test_retention_loop_repeats_survives_failures_and_stops_on_cancel(app, settings, monkeypatch):
    calls = {"n": 0}

    def flaky_run(_settings):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")

    monkeypatch.setattr(maintenance, "run_retention_once", flaky_run)
    fast = settings.model_copy(update={"log_retention_interval_hours": 1})

    async def scenario() -> None:
        async def instant_sleep(_seconds):
            await real_sleep(0)

        real_sleep = asyncio.sleep
        monkeypatch.setattr(maintenance.asyncio, "sleep", instant_sleep)
        task = asyncio.create_task(maintenance.retention_loop(fast))
        while calls["n"] < 3:
            await real_sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.cancelled()

    asyncio.run(scenario())
    assert calls["n"] >= 3  # the first run failed and the loop carried on


def test_purge_logs_command_dry_run_then_real(settings):
    _seed_old_rows_via_cli_db(settings)
    runner = CliRunner()

    r = runner.invoke(main, ["purge-logs", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "Would delete 1 audit log(s)" in r.output
    assert "Would delete 1 connection log(s)" in r.output
    with get_session_factory()() as db:
        assert db.execute(select(ConnectionLog)).first() is not None

    r = runner.invoke(main, ["purge-logs"])
    assert r.exit_code == 0, r.output
    assert "Deleted 1 audit log(s)" in r.output
    with get_session_factory()() as db:
        assert db.execute(select(ConnectionLog)).first() is None
        detail = db.execute(
            select(AuditLog.detail).where(AuditLog.action == "log_retention_purge")
        ).scalar_one()
        assert json.loads(detail)["connection_logs"] == 1


def test_purge_logs_command_reports_disabled_retention(settings, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_RETENTION_DAYS", "0")
    monkeypatch.setenv("CONNECTION_LOG_RETENTION_DAYS", "0")
    clear_settings_cache()
    _seed_old_rows_via_cli_db(get_settings())

    r = CliRunner().invoke(main, ["purge-logs"])
    assert r.exit_code == 0, r.output
    assert "never - retention disabled" in r.output
    with get_session_factory()() as db:
        assert db.execute(select(ConnectionLog)).first() is not None


def _seed_old_rows_via_cli_db(settings) -> None:
    from rustdesk_api.db.database import init_engine
    from rustdesk_api.db.migrations.runner import run_migrations

    init_engine(settings)
    run_migrations(settings)
    _seed_old_rows()
