from __future__ import annotations

import datetime
import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command

from rustdesk_api.config import clear_settings_cache, get_settings
from rustdesk_api.db.database import init_engine
from rustdesk_api.db.migrations.runner import get_alembic_config, pending_migration, run_migrations
from rustdesk_api.services import backup

PREVIOUS_REVISION = "c2e7a9d1f4b6"


@pytest.fixture()
def database(app, settings) -> Path:
    """The migrated test database (the `app` fixture copied the template into place)."""
    path = backup.sqlite_path(settings)
    assert path is not None and path.exists()
    return path


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


def _settings_with(monkeypatch, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    clear_settings_cache()
    return get_settings()


def test_a_backup_is_a_complete_copy_of_the_database(settings, database):
    created = backup.create_backup(settings)

    assert created.parent == database.parent / "backups"
    assert created.name.startswith("rustdesk-") and created.suffix == ".db"
    assert _tables(created) == _tables(database)
    # Nothing half-written is left behind.
    assert not list(created.parent.glob("*.partial"))


def test_two_backups_in_the_same_second_get_different_names(settings, database):
    first = backup.create_backup(settings)
    second = backup.create_backup(settings)
    assert first != second and first.exists() and second.exists()


def test_an_explicit_destination_is_never_overwritten(settings, database, tmp_path):
    target = tmp_path / "mine.db"
    backup.create_backup(settings, destination=target)
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.create_backup(settings, destination=target)


def test_backups_need_sqlite(settings, monkeypatch):
    other = _settings_with(monkeypatch, DATABASE_URL="postgresql://u:p@host/db")
    with pytest.raises(backup.BackupError, match="SQLite"):
        backup.create_backup(other)
    assert backup.list_backups(other) == []
    assert backup.run_scheduled_backup(other) is None


def test_a_missing_database_is_reported(settings):
    path = backup.sqlite_path(settings)
    assert path is not None
    path.unlink(missing_ok=True)
    with pytest.raises(backup.BackupError, match="not found"):
        backup.create_backup(settings)


def test_listing_tells_the_kinds_apart_and_ignores_other_files(settings, database):
    manual = backup.create_backup(settings)
    auto = backup.create_backup(settings, kind=backup.AUTO)
    pre = backup.create_backup(settings, kind=backup.PREMIGRATE)
    (manual.parent / "notes.txt").write_text("x")
    (manual.parent / "rustdesk-oops.db").write_text("x")  # not a timestamp

    kinds = {b.name: b.kind for b in backup.list_backups(settings)}
    assert kinds == {manual.name: "manual", auto.name: "auto", pre.name: "premigrate"}


def test_rotation_keeps_the_newest_scheduled_backups_and_never_touches_manual_ones(
    settings, database, monkeypatch
):
    limited = _settings_with(monkeypatch, BACKUP_KEEP="2")

    manual = backup.create_backup(limited)
    for age in (40, 30, 20, 10):  # oldest first, by modification time
        path = backup.create_backup(limited, kind=backup.AUTO)
        stamp = datetime.datetime.now().timestamp() - age * 3600
        os.utime(path, (stamp, stamp))
    backup.rotate(limited, backup.AUTO)

    remaining = [b for b in backup.list_backups(limited) if b.kind == "auto"]
    assert len(remaining) == 2
    # The two that were kept are the newest ones (about 10 and 20 hours old).
    now = datetime.datetime.now(datetime.timezone.utc)
    assert all(now - b.created_at < datetime.timedelta(hours=21) for b in remaining)
    assert manual.exists()


def test_a_scheduled_backup_is_made_only_when_the_last_one_is_old_enough(settings, database, monkeypatch):
    scheduled = _settings_with(monkeypatch, BACKUP_INTERVAL_HOURS="24")

    first = backup.run_scheduled_backup(scheduled)
    assert first is not None and first.name.startswith("rustdesk-auto-")
    # A restart an hour later does not add another.
    soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    assert backup.run_scheduled_backup(scheduled, now=soon) is None
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=25)
    assert backup.run_scheduled_backup(scheduled, now=later) is not None


def test_the_schedule_can_be_switched_off(settings, database):
    assert settings.backup_interval_hours == 0  # the test fixture turns it off
    assert backup.run_scheduled_backup(settings) is None


def _database_at_the_previous_revision(settings) -> None:
    init_engine(settings)
    command.upgrade(get_alembic_config(settings), PREVIOUS_REVISION)


def test_migrating_an_older_database_takes_a_backup_first(settings):
    _database_at_the_previous_revision(settings)
    assert pending_migration(settings) is not None
    assert backup.list_backups(settings) == []

    run_migrations(settings)

    saved = backup.list_backups(settings)
    assert [b.kind for b in saved] == ["premigrate"]
    # The copy is the database as it was: without the columns the migration added.
    connection = sqlite3.connect(backup.backup_directory(settings) / saved[0].name)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(devices)")}
    finally:
        connection.close()
    assert "api_scheme" in columns and "archived_at" not in columns
    # Once at head there is nothing more to back up.
    assert pending_migration(settings) is None
    run_migrations(settings)
    assert len(backup.list_backups(settings)) == 1


def test_a_new_database_is_not_backed_up(settings):
    run_migrations(settings)
    assert backup.list_backups(settings) == []


def test_the_pre_migration_backup_can_be_switched_off(settings, monkeypatch):
    relaxed = _settings_with(monkeypatch, BACKUP_BEFORE_MIGRATION="false")
    _database_at_the_previous_revision(relaxed)
    run_migrations(relaxed)
    assert backup.list_backups(relaxed) == []


def test_a_failed_pre_migration_backup_stops_the_migration(settings, monkeypatch):
    _database_at_the_previous_revision(settings)

    def broken(*_args, **_kwargs):
        raise backup.BackupError("The backup could not be written: disk full")

    monkeypatch.setattr(backup, "create_backup", broken)
    with pytest.raises(backup.BackupError, match="not migrated"):
        run_migrations(settings)
    # Still at the old revision: nothing was changed without the safety copy.
    pending = pending_migration(settings)
    assert pending is not None and pending[0] == PREVIOUS_REVISION
