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

from pathlib import Path

from alembic import command
from alembic.config import Config

from rustdesk_api.config import Settings

MIGRATIONS_DIR = Path(__file__).resolve().parent


def get_alembic_config(settings: Settings) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def run_migrations(settings: Settings) -> None:
    cfg = get_alembic_config(settings)
    command.upgrade(cfg, "head")
