"""The report of environment-variable options behind the Settings page."""

from __future__ import annotations

import re
from pathlib import Path

from rustdesk_api.config import Settings
from rustdesk_api.services import options


def _settings(**changes) -> Settings:
    return Settings.model_construct().model_copy(update=changes)


def _by_env(settings: Settings) -> dict[str, options.Option]:
    return {o.env[0]: o for o in options.report(settings)}


def test_every_environment_variable_of_the_settings_is_reported():
    """Given a setting added to config.py, when it is not in the report, then this fails - so the
    page cannot silently fall behind the server's options."""
    aliases = {field.alias for field in Settings.model_fields.values() if field.alias}
    missing = aliases - options.covered_variables()
    assert not missing, f"Add these to services/options.py (or NOT_LISTED): {sorted(missing)}"


def test_no_variable_is_reported_twice():
    seen: list[str] = []
    for option in options.report(_settings()):
        seen.extend(option.env)
    assert len(seen) == len(set(seen)), sorted({n for n in seen if seen.count(n) > 1})


def test_only_real_variables_are_named():
    aliases = {field.alias for field in Settings.model_fields.values() if field.alias}
    unknown = {name for o in options.report(_settings()) for name in o.env} - aliases
    assert not unknown


def test_the_groups_come_in_a_fixed_order():
    groups = [o.group for o in options.report(_settings())]
    assert [g for g in options.GROUPS if g in groups] == list(dict.fromkeys(groups))


def test_an_option_that_is_not_set_is_off_and_one_that_is_set_is_on():
    off = _by_env(_settings())
    assert off["ALLOW_REGISTRATION"].enabled is False
    assert off["WEBUI_ALLOWED_NETWORKS"].enabled is False
    assert off["METRICS_TOKEN"].enabled is False
    assert off["DATA_ENCRYPTION_KEY"].enabled is False
    assert off["SECRET_KEY"].enabled is False  # still the "change-me" placeholder

    on = _by_env(
        _settings(
            allow_registration=True,
            webui_allowed_networks="10.0.0.0/8, 192.168.0.0/16",
            metrics_token="x" * 20,
            data_encryption_key="k",
            secret_key="a-long-random-secret-key",
        )
    )
    assert on["ALLOW_REGISTRATION"].enabled is True
    assert on["WEBUI_ALLOWED_NETWORKS"].enabled is True
    assert on["WEBUI_ALLOWED_NETWORKS"].args == (2,)
    assert on["METRICS_TOKEN"].enabled is True
    assert on["DATA_ENCRYPTION_KEY"].enabled is True
    assert on["SECRET_KEY"].enabled is True


def test_zero_switches_off_the_options_where_zero_means_never():
    off = _by_env(
        _settings(
            login_lockout_threshold=0,
            device_stale_days=0,
            audit_log_retention_days=0,
            connection_log_retention_days=0,
            backup_interval_hours=0,
        )
    )
    assert off["LOGIN_LOCKOUT_THRESHOLD"].enabled is False
    assert off["DEVICE_STALE_DAYS"].enabled is False
    assert off["AUDIT_LOG_RETENTION_DAYS"].enabled is False
    assert off["CONNECTION_LOG_RETENTION_DAYS"].enabled is False
    assert off["BACKUP_INTERVAL_HOURS"].enabled is False
    assert off["BACKUP_INTERVAL_HOURS"].detail is None


def test_the_take_over_protection_is_off_only_in_allow_mode():
    assert _by_env(_settings(device_uuid_rebind="approve"))["DEVICE_UUID_REBIND"].enabled is True
    assert _by_env(_settings(device_uuid_rebind="deny"))["DEVICE_UUID_REBIND"].enabled is True
    allow = _by_env(_settings(device_uuid_rebind="allow"))["DEVICE_UUID_REBIND"]
    assert allow.enabled is False and allow.args == ("allow",)


def test_single_sign_on_needs_both_the_issuer_and_the_client_id():
    assert _by_env(_settings(oidc_issuer="https://idp.example.com"))["OIDC_ISSUER"].enabled is False
    both = _by_env(_settings(oidc_issuer="https://idp.example.com", oidc_client_id="abc", oidc_name="corp"))[
        "OIDC_ISSUER"
    ]
    assert both.enabled is True and both.args == ("corp",)


def test_secrets_are_reported_as_set_and_never_shown():
    secrets = {
        "notify_webhook_url": "https://hooks.example.com/services/T0/B0/webhook-secret-token",
        "notify_webhook_secret": "signing-secret-value",
        "notify_ntfy_url": "https://ntfy.example.com/private-topic-name",
        "notify_ntfy_token": "ntfy-token-value",
        "notify_smtp_host": "smtp.example.com",
        "notify_smtp_password": "smtp-password-value",
        "notify_email_from": "alerts@example.com",
        "notify_email_to": "ops@example.com",
        "metrics_token": "metrics-token-value-1234",
        "oidc_issuer": "https://idp.example.com",
        "oidc_client_id": "client-id-value",
        "oidc_client_secret": "oidc-client-secret-value",
        "data_encryption_key": "encryption-key-value",
        "secret_key": "the-server-secret-key-value",
        "rustdesk_key": "rustdesk-public-key-value",
        "installer_sign_command": 'signtool sign /p "pfx-password-value" "%1"',
        "installer_dir": "/srv/private/installers",
        "installer_icon": "/srv/private/icon.ico",
        "backup_dir": "/srv/private/backups",
        "database_url": "postgresql://dbuser:database-password-value@db.example.com/rd",
        "ssl_certfile": "/srv/private/cert.pem",
        "ssl_keyfile": "/srv/private/key.pem",
    }
    report = options.report(_settings(**secrets))
    text = repr([(o.group, o.env, o.label, o.help, o.enabled, o.detail, o.args) for o in report])
    for name, value in secrets.items():
        for piece in re.split(r"[/@: ]", value):
            # A host name is shown on purpose (where a channel sends to); anything else is not.
            if len(piece) > 12 and not piece.endswith("example.com"):
                assert piece not in text, f"{name} leaked into the report"
    # The parts that are not secret are: a host of a channel, the kind of database.
    by_env = {o.env[0]: o for o in report}
    assert by_env["NOTIFY_WEBHOOK_URL"].args == ("hooks.example.com",)
    assert by_env["DATABASE_URL"].args == ("postgresql",)


def test_the_session_lifetime_is_told_in_days_when_it_is_whole_days():
    week = _by_env(_settings(session_lifetime_seconds=604800))["SESSION_LIFETIME_SECONDS"]
    assert (week.detail, week.args) == ("{1} day(s)", (7,))
    ninety_minutes = _by_env(_settings(session_lifetime_seconds=5400))["SESSION_LIFETIME_SECONDS"]
    assert ninety_minutes.detail == "{1} hour(s)"


def _catalog_keys() -> set[str]:
    root = Path(__file__).resolve().parents[2] / "frontend" / "i18n"
    keys: set[str] = set()
    for path in root.glob("*.txt"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                keys.add(line.split(" || ")[0].lstrip("!").strip())
    return keys


def test_every_text_of_the_report_is_in_the_translation_catalog():
    """The page shows these texts; without a catalog line they stay English in the other languages."""
    busy = _settings(
        allow_registration=True,
        registration_requires_approval=True,
        oidc_issuer="https://idp.example.com",
        oidc_client_id="abc",
        webui_allowed_networks="10.0.0.0/8",
        trusted_proxies="10.0.0.1",
        cors_origins="https://a.example.com",
        rustdesk_id_server="id.example.com",
        device_stale_days=30,
        min_client_version="1.4.0",
        data_encryption_key="k",
        notify_webhook_url="https://hooks.example.com/x",
        notify_ntfy_url="https://ntfy.example.com/x",
        notify_smtp_host="smtp.example.com",
        notify_email_from="a@example.com",
        notify_email_to="b@example.com",
        session_lifetime_seconds=5400,
    )
    other = _settings(
        allow_registration=True, registration_requires_approval=False, installer_makensis="/none"
    )
    catalog = _catalog_keys()
    missing: set[str] = set()
    for settings in (busy, other, _settings()):
        for option in options.report(settings):
            texts = [option.group, option.label, option.help]
            # A detail that is only values ("{1}", "{1}:{2}") has nothing to translate.
            if option.detail and re.sub(r"\{\d\}|[:\s]", "", option.detail):
                texts.append(option.detail)
            missing |= {t for t in texts if t not in catalog}
    assert not missing, sorted(missing)
