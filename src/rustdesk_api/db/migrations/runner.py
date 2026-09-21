"""Programmatic Alembic runner.

Used both by the CLI (`rustdesk-api migrate`) and at app startup so a clean
checkout works with just `python -m rustdesk_api` (CLAUDE.md section 55)
without requiring a separate manual migration step. Running
`alembic upgrade head` against an up-to-date database is a no-op, so this
is safe to call unconditionally on every startup.

Deliberately does not depend on the repo-root `alembic.ini` file (kept
there only for developers running the `alembic` CLI directly, e.g. to
autogenerate a new revision). Locating it via the installed package's
__file__ would break once the package is installed non-editably into
site-packages (as in the Docker image), where there is no "repo root" to
find. `script_location` is resolved package-relative instead, which is
correct in every install layout.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool

from rustdesk_api.config import Settings
from rustdesk_api.services import backup as backup_service

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent


def get_alembic_config(settings: Settings) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def pending_migration(settings: Settings) -> tuple[str, str] | None:
    """(current revision, newest revision) when an existing database is behind the
    code, else None. A database that does not exist yet, or has no revision (a new
    one), has nothing worth backing up."""
    path = backup_service.sqlite_path(settings)
    if path is None or not path.exists():
        return None
    head = ScriptDirectory.from_config(get_alembic_config(settings)).get_current_head()
    engine = create_engine(settings.database_url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    if current is None or current == head:
        return None
    return current, head or ""


def _backup_before_upgrade(settings: Settings) -> None:
    pending = pending_migration(settings)
    if pending is None:
        return
    logger.info("the database is at revision %s and the code at %s: backing it up before migrating", *pending)
    try:
        path = backup_service.create_backup(settings, kind=backup_service.PREMIGRATE)
    except backup_service.BackupError as exc:
        # Migrating without the safety copy is what this setting exists to prevent, so
        # stop and say how to go on, rather than carrying on silently.
        raise backup_service.BackupError(
            f"{exc} The database was not migrated. Fix the problem, or set "
            "BACKUP_BEFORE_MIGRATION=false to migrate without a backup."
        ) from exc
    logger.info("pre-migration backup written to %s", path.name)


def run_migrations(settings: Settings) -> None:
    if settings.backup_before_migration:
        _backup_before_upgrade(settings)
    cfg = get_alembic_config(settings)
    command.upgrade(cfg, "head")
