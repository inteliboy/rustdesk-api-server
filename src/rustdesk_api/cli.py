"""Command-line interface.

Every management task must be doable without hand-editing the SQLite
database (CLAUDE.md section 52).
"""

from __future__ import annotations

import datetime
import getpass
import sqlite3
import sys
from pathlib import Path

import click
import uvicorn
from sqlalchemy import select

from rustdesk_api import __version__
from rustdesk_api.app import configure_logging
from rustdesk_api.config import Settings, get_settings
from rustdesk_api.db.database import init_engine, session_scope
from rustdesk_api.db.migrations.runner import run_migrations
from rustdesk_api.models.user import User
from rustdesk_api.security.encryption import generate_key, get_secret_box
from rustdesk_api.services import address_book as address_book_service
from rustdesk_api.services import authentication as auth_service
from rustdesk_api.services import retention as retention_service
from rustdesk_api.services import tokens as token_service
from rustdesk_api.services import two_factor as two_factor_service

_SQLITE_PREFIX = "sqlite:///"


def _bootstrap_db(settings: Settings) -> None:
    """Every command that touches the database goes through this, so none
    of them can be run against a not-yet-migrated database (CLAUDE.md
    section 55: the application must create its schema automatically)."""
    init_engine(settings)
    run_migrations(settings)


@click.group()
def main() -> None:
    """RustDesk API server management commands."""


@main.command()
@click.option("--host", default=None, help="Override RUSTDESK_API_HOST")
@click.option("--port", default=None, type=int, help="Override RUSTDESK_API_PORT")
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload (development only)")
@click.option("--ssl-certfile", default=None, help="Override SSL_CERTFILE (enables HTTPS)")
@click.option("--ssl-keyfile", default=None, help="Override SSL_KEYFILE (enables HTTPS)")
def serve(
    host: str | None,
    port: int | None,
    reload: bool,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
) -> None:
    """Run the API server and WebUI."""
    settings = get_settings()
    uvicorn.run(
        "rustdesk_api.app:create_app",
        factory=True,
        host=host or settings.rustdesk_api_host,
        port=port or settings.rustdesk_api_port,
        reload=reload,
        log_config=None,
        ssl_certfile=ssl_certfile or settings.ssl_certfile or None,
        ssl_keyfile=ssl_keyfile or settings.ssl_keyfile or None,
    )


@main.command()
def migrate() -> None:
    """Apply database migrations (alembic upgrade head)."""
    settings = get_settings()
    configure_logging(settings)
    _bootstrap_db(settings)
    click.echo("Migrations applied.")


@main.command()
@click.option(
    "--output",
    "output_path",
    default=None,
    help="Backup file path (default: <db directory>/backups/rustdesk-<timestamp>.db)",
)
def backup(output_path: str | None) -> None:
    """Back up the live SQLite database using SQLite's own VACUUM INTO.

    Unlike a plain file copy, VACUUM INTO produces a consistent snapshot
    while the database is open and being written to (e.g. by a running
    `rustdesk-api serve`) - CLAUDE.md section 38 explicitly warns against
    telling users that copying an actively-written SQLite file is always
    safe, and asks for a SQLite-aware mechanism where practical."""
    settings = get_settings()
    if not settings.database_url.startswith(_SQLITE_PREFIX):
        raise click.ClickException("backup only supports SQLite (DATABASE_URL must start with sqlite:///).")

    source_path = Path(settings.database_url[len(_SQLITE_PREFIX) :])
    if not source_path.exists():
        raise click.ClickException(f"Database file not found: {source_path}")

    if output_path:
        dest_path = Path(output_path)
    else:
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest_path = source_path.parent / "backups" / f"rustdesk-{timestamp}.db"

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists():
        raise click.ClickException(f"{dest_path} already exists; refusing to overwrite.")

    # Autocommit (isolation_level=None) so this connection never wraps
    # VACUUM in an implicit transaction - SQLite rejects VACUUM inside one.
    connection = sqlite3.connect(str(source_path), isolation_level=None)
    try:
        connection.execute("VACUUM INTO ?", (str(dest_path),))
    finally:
        connection.close()

    click.echo(f"Backup written to {dest_path}")


@main.command("create-admin")
@click.option("--username", prompt=True)
@click.option("--email", default=None)
def create_admin(username: str, email: str | None) -> None:
    """Create an administrator account interactively."""
    settings = get_settings()
    _bootstrap_db(settings)

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise click.ClickException("Passwords do not match.")
    if len(password) < 8:
        raise click.ClickException("Password must be at least 8 characters.")

    with session_scope() as db:
        if auth_service.get_user_by_username(db, username) is not None:
            raise click.ClickException(f"User {username!r} already exists.")
        auth_service.create_user(db, username=username, password=password, email=email, is_admin=True)
    click.echo(f"Administrator {username!r} created.")


@main.command("reset-password")
@click.option("--username", prompt=True)
def reset_password(username: str) -> None:
    """Reset a user's password."""
    settings = get_settings()
    _bootstrap_db(settings)

    password = getpass.getpass("New password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise click.ClickException("Passwords do not match.")
    if len(password) < 8:
        raise click.ClickException("Password must be at least 8 characters.")

    with session_scope() as db:
        user = auth_service.get_user_by_username(db, username)
        if user is None:
            raise click.ClickException(f"User {username!r} not found.")
        auth_service.set_password(db, user, password)
    click.echo(f"Password reset for {username!r}.")


@main.command("disable-2fa")
@click.option("--username", prompt=True)
def disable_2fa(username: str) -> None:
    """Turn two-factor authentication off for a user (lost authenticator, lost
    DATA_ENCRYPTION_KEY) and sign them out everywhere. Nothing is written to the
    audit log from here, like `reset-password`."""
    settings = get_settings()
    _bootstrap_db(settings)
    with session_scope() as db:
        user = auth_service.get_user_by_username(db, username)
        if user is None:
            raise click.ClickException(f"User {username!r} not found.")
        if not user.totp_enabled and not user.totp_secret_enc:
            click.echo(f"Two-factor authentication is not on for {username!r}.")
            return
        two_factor_service.disable(db, user)
        token_service.revoke_all_for_user(db, user.id)
    click.echo(f"Two-factor authentication turned off for {username!r}; their sessions were ended.")


@main.command("unlock-user")
@click.option("--username", prompt=True)
def unlock_user(username: str) -> None:
    """Lift a lockout caused by too many wrong passwords."""
    settings = get_settings()
    _bootstrap_db(settings)
    with session_scope() as db:
        user = auth_service.get_user_by_username(db, username)
        if user is None:
            raise click.ClickException(f"User {username!r} not found.")
        auth_service.unlock(user)
    click.echo(f"{username!r} is unlocked.")


@main.command("list-users")
def list_users() -> None:
    """List all users."""
    settings = get_settings()
    _bootstrap_db(settings)
    with session_scope() as db:
        for user in db.execute(select(User).order_by(User.username)).scalars():
            role = "admin" if user.is_admin else "user"
            status = "active" if user.is_active else "disabled"
            if auth_service.is_locked(user):
                status += " (locked)"
            click.echo(f"{user.id}\t{user.username}\t{role}\t{status}")


@main.command("purge-logs")
@click.option("--dry-run", is_flag=True, default=False, help="Only report what would be deleted")
def purge_logs(dry_run: bool) -> None:
    """Delete logs older than the configured retention now.

    Uses AUDIT_LOG_RETENTION_DAYS and CONNECTION_LOG_RETENTION_DAYS (0 keeps
    that kind forever). The server does this on its own every
    LOG_RETENTION_INTERVAL_HOURS; this is for an immediate run."""
    settings = get_settings()
    configure_logging(settings)
    _bootstrap_db(settings)
    with session_scope() as db:
        result = retention_service.purge_expired(db, settings, dry_run=dry_run)
    verb = "Would delete" if dry_run else "Deleted"
    click.echo(
        f"{verb} {result.audit_logs} audit log(s) (older than {_days(settings.audit_log_retention_days)})."
    )
    click.echo(
        f"{verb} {result.connection_logs} connection log(s), {result.file_transfer_logs} "
        f"file transfer log(s) and {result.alarm_logs} alarm log(s) "
        f"(older than {_days(settings.connection_log_retention_days)})."
    )
    if not dry_run:
        click.echo(f"Removed {result.sessions} expired login session(s).")


def _days(days: int) -> str:
    return f"{days} day(s)" if days > 0 else "never - retention disabled"


@main.command("check-config")
def check_config() -> None:
    """Print resolved configuration (secrets redacted) and validate it."""
    settings = get_settings()
    redacted = settings.model_dump()
    for key in ("secret_key", "rustdesk_key", "data_encryption_key", "metrics_token"):
        if redacted.get(key):
            redacted[key] = "***redacted***"
    for key, value in redacted.items():
        click.echo(f"{key}={value}")
    if settings.secret_key == "change-me":
        click.echo("\nWARNING: SECRET_KEY is still the default value. Set a real secret in production.")
    if not settings.data_encryption_key:
        click.echo(
            "\nNOTE: DATA_ENCRYPTION_KEY is not set, so shared address book passwords are not saved and "
            "two-factor authentication cannot be turned on. "
            "Generate a key with `rustdesk-api generate-key`."
        )
    if settings.webui_allowed_network_list:
        click.echo(
            "\nNOTE: WEBUI_ALLOWED_NETWORKS is set: the WebUI and /api/v1 answer only to "
            f"{', '.join(str(n) for n in settings.webui_allowed_network_list)}. Behind a reverse proxy, "
            "TRUSTED_PROXIES must list it or every request looks like it comes from the proxy."
        )
    if settings.allow_registration and not settings.registration_requires_approval:
        click.echo(
            "\nNOTE: ALLOW_REGISTRATION is on without REGISTRATION_REQUIRES_APPROVAL: anyone who can "
            "reach this server can create an account that can sign in immediately."
        )


@main.command("generate-key")
def generate_key_command() -> None:
    """Print a new DATA_ENCRYPTION_KEY."""
    click.echo(generate_key())


@main.command("rotate-data-key")
def rotate_data_key() -> None:
    """Re-encrypt stored secrets with the first key in DATA_ENCRYPTION_KEY.

    Put the new key first and keep the old one after it (comma separated), run
    this, then drop the old key.
    """
    settings = get_settings()
    box = get_secret_box(settings.data_encryption_key)
    if box is None:
        raise click.ClickException("DATA_ENCRYPTION_KEY is not set.")
    _bootstrap_db(settings)
    with session_scope() as db:
        rotated, unreadable = address_book_service.rotate_passwords(db, box)
        secrets_rotated, secrets_unreadable = two_factor_service.rotate_secrets(db, box)
    click.echo(f"Re-encrypted {rotated} stored password(s) and {secrets_rotated} two-factor secret(s).")
    if unreadable or secrets_unreadable:
        click.echo(
            f"{unreadable} stored password(s) and {secrets_unreadable} two-factor secret(s) could not be "
            "decrypted with the configured key(s) and were left unchanged.",
            err=True,
        )
        sys.exit(1)


@main.command()
def version() -> None:
    """Print the application version."""
    click.echo(__version__)
