"""CLI tests using Click's test runner (isolated stdin/stdout, no TTY
needed - see CLAUDE.md section 53: password prompts must not echo)."""

from pathlib import Path
from sqlite3 import connect

from click.testing import CliRunner

from rustdesk_api.cli import main
from rustdesk_api.config import Settings


def _runner(settings) -> CliRunner:
    return CliRunner()


def test_list_users_on_empty_database(settings):
    r = _runner(settings).invoke(main, ["list-users"])
    assert r.exit_code == 0


def test_create_admin_then_list_users(settings):
    runner = _runner(settings)
    r = runner.invoke(main, ["create-admin", "--username", "admin"], input="adminpass123\nadminpass123\n")
    assert r.exit_code == 0, r.output
    assert "created" in r.output

    r = runner.invoke(main, ["list-users"])
    assert "admin" in r.output


def test_create_admin_rejects_mismatched_passwords(settings):
    runner = _runner(settings)
    r = runner.invoke(main, ["create-admin", "--username", "admin"], input="passwordone1\npasswordtwo2\n")
    assert r.exit_code != 0


def test_create_admin_twice_fails_for_same_username(settings):
    runner = _runner(settings)
    runner.invoke(main, ["create-admin", "--username", "admin"], input="adminpass123\nadminpass123\n")
    r = runner.invoke(main, ["create-admin", "--username", "admin"], input="otherpass123\notherpass123\n")
    assert r.exit_code != 0


def test_reset_password_updates_credentials(settings):
    runner = _runner(settings)
    runner.invoke(main, ["create-admin", "--username", "admin"], input="adminpass123\nadminpass123\n")
    r = runner.invoke(main, ["reset-password", "--username", "admin"], input="newpassword1\nnewpassword1\n")
    assert r.exit_code == 0
    assert "reset" in r.output


def test_check_config_never_prints_secret_key(settings):
    r = _runner(settings).invoke(main, ["check-config"])
    assert r.exit_code == 0
    assert "test-secret-key" not in r.output
    assert "redacted" in r.output


def test_version_command(settings):
    r = _runner(settings).invoke(main, ["version"])
    assert r.exit_code == 0
    assert r.output.strip()


def test_backup_creates_a_restorable_snapshot(settings):
    runner = _runner(settings)
    runner.invoke(main, ["create-admin", "--username", "admin"], input="adminpass123\nadminpass123\n")

    assert isinstance(settings, Settings)
    dest = Path(settings.database_url[len("sqlite:///") :]).parent / "manual-backup.db"
    r = runner.invoke(main, ["backup", "--output", str(dest)])
    assert r.exit_code == 0, r.output
    assert dest.exists()

    conn = connect(str(dest))
    try:
        (username,) = conn.execute("SELECT username FROM users").fetchone()
    finally:
        conn.close()
    assert username == "admin"


def test_backup_refuses_to_overwrite_existing_file(settings, tmp_path):
    runner = _runner(settings)
    runner.invoke(main, ["create-admin", "--username", "admin"], input="adminpass123\nadminpass123\n")

    dest = tmp_path / "already-here.db"
    dest.write_bytes(b"not a real backup")
    r = runner.invoke(main, ["backup", "--output", str(dest)])
    assert r.exit_code != 0
    assert "already exists" in r.output


def test_backup_default_path_lands_under_backups_dir(settings):
    runner = _runner(settings)
    runner.invoke(main, ["create-admin", "--username", "admin"], input="adminpass123\nadminpass123\n")

    assert isinstance(settings, Settings)
    db_dir = Path(settings.database_url[len("sqlite:///") :]).parent
    r = runner.invoke(main, ["backup"])
    assert r.exit_code == 0, r.output

    backups = list((db_dir / "backups").glob("rustdesk-*.db"))
    assert len(backups) == 1
