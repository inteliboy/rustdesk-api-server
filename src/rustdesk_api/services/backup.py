"""Consistent SQLite backups: on demand, on a schedule and before a migration.

Every backup is made with SQLite's own `VACUUM INTO`, which produces a consistent
snapshot while the database is open and being written to (CLAUDE.md section 38: a
plain copy of a live SQLite file is not always safe). A backup is written under a
temporary name and renamed when complete, so a crash never leaves a half-written
file that looks like a backup.

Three kinds share one directory, told apart by their file name:

* `rustdesk-<time>.db`             manual (the `backup` command, "Back up now"); never deleted
* `rustdesk-auto-<time>.db`        scheduled; the newest BACKUP_KEEP are kept
* `rustdesk-premigrate-<time>.db`  taken before a migration; the newest few are kept
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from rustdesk_api.config import Settings

logger = logging.getLogger(__name__)

_SQLITE_PREFIX = "sqlite:///"

MANUAL = "manual"
AUTO = "auto"
PREMIGRATE = "premigrate"

_PREFIX = {MANUAL: "rustdesk-", AUTO: "rustdesk-auto-", PREMIGRATE: "rustdesk-premigrate-"}
# A migration backup is the only copy of the data as it was before a schema change,
# so a few are kept regardless of BACKUP_KEEP.
PREMIGRATE_KEEP = 5

_TIMESTAMP = re.compile(r"^\d{8}T\d{6}Z(?:-\d+)?$")


class BackupError(Exception):
    """A backup could not be made (unsupported database, no space, ...)."""


@dataclass(frozen=True)
class BackupInfo:
    name: str
    kind: str
    size: int
    created_at: datetime.datetime


def sqlite_path(settings: Settings) -> Path | None:
    """The database file, or None when the database is not SQLite."""
    if not settings.database_url.startswith(_SQLITE_PREFIX):
        return None
    return Path(settings.database_url[len(_SQLITE_PREFIX) :])


def backup_directory(settings: Settings) -> Path:
    if settings.backup_dir.strip():
        return Path(settings.backup_dir.strip())
    source = sqlite_path(settings)
    if source is None:
        raise BackupError("Backups only support SQLite (DATABASE_URL must start with sqlite:///).")
    return source.parent / "backups"


def _kind_of(name: str) -> str | None:
    """The kind a backup file name says it is, or None for a file that is not ours."""
    if not name.endswith(".db"):
        return None
    stem = name[: -len(".db")]
    # Longest prefix first: "rustdesk-auto-" also starts with "rustdesk-".
    for kind in (AUTO, PREMIGRATE, MANUAL):
        prefix = _PREFIX[kind]
        if stem.startswith(prefix) and _TIMESTAMP.match(stem[len(prefix) :]):
            return kind
    return None


def _free_name(directory: Path, kind: str, now: datetime.datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    candidate = directory / f"{_PREFIX[kind]}{stamp}.db"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{_PREFIX[kind]}{stamp}-{counter}.db"
        counter += 1
    return candidate


def create_backup(settings: Settings, *, kind: str = MANUAL, destination: Path | None = None) -> Path:
    """Writes a snapshot of the database and returns its path. With an explicit
    `destination` (the CLI's --output) the name is the caller's, an existing file is
    never overwritten, and nothing is rotated."""
    source = sqlite_path(settings)
    if source is None:
        raise BackupError("Backups only support SQLite (DATABASE_URL must start with sqlite:///).")
    if not source.exists():
        raise BackupError(f"Database file not found: {source}")

    if destination is None:
        directory = backup_directory(settings)
        directory.mkdir(parents=True, exist_ok=True)
        target = _free_name(directory, kind, datetime.datetime.now(datetime.timezone.utc))
    else:
        target = destination
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise BackupError(f"{target} already exists; refusing to overwrite.")

    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    # Autocommit (isolation_level=None) so this connection never wraps VACUUM in an
    # implicit transaction - SQLite rejects VACUUM inside one.
    connection = sqlite3.connect(str(source), isolation_level=None)
    try:
        connection.execute("VACUUM INTO ?", (str(partial),))
    except (sqlite3.Error, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise BackupError(f"The backup could not be written: {exc}") from exc
    finally:
        connection.close()
    os.replace(partial, target)

    if destination is None:
        rotate(settings, kind)
    return target


def rotate(settings: Settings, kind: str) -> list[Path]:
    """Deletes the oldest backups of `kind` beyond the number to keep (manual ones are
    never deleted). Returns what was removed."""
    if kind == MANUAL:
        return []
    keep = settings.backup_keep if kind == AUTO else PREMIGRATE_KEEP
    directory = backup_directory(settings)
    files = sorted((p for p in directory.glob("*.db") if _kind_of(p.name) == kind), key=_sort_key)
    removed: list[Path] = []
    for path in files[: max(len(files) - keep, 0)]:
        try:
            path.unlink()
        except OSError:
            logger.warning("could not delete the old backup %s", path.name)
            continue
        removed.append(path)
    return removed


def _sort_key(path: Path) -> tuple[float, str]:
    try:
        return (path.stat().st_mtime, path.name)
    except OSError:
        return (0.0, path.name)


def list_backups(settings: Settings) -> list[BackupInfo]:
    """Newest first."""
    try:
        directory = backup_directory(settings)
    except BackupError:
        return []
    if not directory.is_dir():
        return []
    found: list[BackupInfo] = []
    for path in directory.glob("*.db"):
        kind = _kind_of(path.name)
        if kind is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append(
            BackupInfo(
                name=path.name,
                kind=kind,
                size=stat.st_size,
                created_at=datetime.datetime.fromtimestamp(stat.st_mtime, datetime.timezone.utc),
            )
        )
    return sorted(found, key=lambda b: (b.created_at, b.name), reverse=True)


def run_scheduled_backup(settings: Settings, *, now: datetime.datetime | None = None) -> Path | None:
    """Makes a scheduled backup when the newest one is older than the interval (so a
    restart does not add one). Returns the new file, or None when none was due."""
    if settings.backup_interval_hours <= 0 or sqlite_path(settings) is None:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    interval = datetime.timedelta(hours=settings.backup_interval_hours)
    newest = next((b for b in list_backups(settings) if b.kind == AUTO), None)
    if newest is not None and now - newest.created_at < interval:
        return None
    path = create_backup(settings, kind=AUTO)
    logger.info("scheduled database backup written to %s", path.name)
    return path
