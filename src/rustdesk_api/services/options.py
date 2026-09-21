# ruff: noqa: E501  (the help texts are sentences shown on the Settings page; wrapping them would only hide them)
"""The options an administrator can set with environment variables, and whether each is on.

This feeds the Settings page. It is a read-only report: settings can hold secrets and the
server is not reconfigured from a browser session. Nothing secret is ever put in a report:
a secret (a key, token, password or a URL that carries one) is only reported as set or not,
and a detail is a plain fact such as a number of days or a host name."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from rustdesk_api.config import Settings
from rustdesk_api.services import installer as installer_service
from rustdesk_api.services import notifications as notification_service
from rustdesk_api.services import signing as signing_service
from rustdesk_api.services import webclient as webclient_service

# Names that are not options an administrator switches: reported nowhere on purpose.
NOT_LISTED = frozenset(
    {
        # Set by the Docker image to the commit it was built from; shown on the Dashboard.
        "GIT_COMMIT",
    }
)


@dataclass(frozen=True)
class Option:
    group: str
    # The environment variables that control it (the first is the main one).
    env: tuple[str, ...]
    label: str
    help: str
    enabled: bool
    # A short sentence with {1}, {2} placeholders, and the values for them. Never a secret.
    detail: str | None = None
    args: tuple[str | int, ...] = ()


ACCOUNTS = "Sign-in and accounts"
NETWORK = "Network and access"
DEVICES = "Devices and clients"
DATA = "Data, logs and backups"
NOTIFICATIONS = "Notifications"
INSTALLER = "Windows installer"
SERVER = "This server"

GROUPS = (ACCOUNTS, NETWORK, DEVICES, DATA, NOTIFICATIONS, INSTALLER, SERVER)


def _count(value: str) -> int:
    return len([p for p in value.split(",") if p.strip()])


def _hours(seconds: int) -> int:
    return max(1, round(seconds / 3600))


def _accounts(s: Settings) -> list[Option]:
    sso = bool(s.oidc_issuer and s.oidc_client_id)
    return [
        Option(
            ACCOUNTS,
            ("ALLOW_REGISTRATION", "REGISTRATION_REQUIRES_APPROVAL"),
            "Self-registration",
            "People can create their own account on the sign-in page.",
            s.allow_registration,
            (
                "an administrator must approve a new account"
                if s.registration_requires_approval
                else "a new account can sign in at once"
            )
            if s.allow_registration
            else None,
        ),
        Option(
            ACCOUNTS,
            (
                "OIDC_ISSUER",
                "OIDC_CLIENT_ID",
                "OIDC_CLIENT_SECRET",
                "OIDC_NAME",
                "OIDC_SCOPES",
                "OIDC_AUTO_CREATE_USERS",
                "OIDC_LINK_BY_EMAIL",
                "OIDC_ALLOWED_EMAIL_DOMAINS",
                "OIDC_USERNAME_CLAIM",
                "OIDC_TIMEOUT_SECONDS",
            ),
            "Single sign-on (OpenID Connect)",
            "Sign in with an identity provider, in the WebUI and in the RustDesk client.",
            sso,
            "button named {1}" if sso else None,
            (s.oidc_name,) if sso else (),
        ),
        Option(
            ACCOUNTS,
            ("LOGIN_LOCKOUT_THRESHOLD", "LOGIN_LOCKOUT_MINUTES"),
            "Account lockout",
            "An account is locked for a while after too many wrong passwords in a row.",
            s.login_lockout_threshold > 0,
            "after {1} wrong passwords, for {2} minute(s)" if s.login_lockout_threshold > 0 else None,
            (s.login_lockout_threshold, s.login_lockout_minutes) if s.login_lockout_threshold > 0 else (),
        ),
        Option(
            ACCOUNTS,
            ("AUTH_RATE_LIMIT_ATTEMPTS", "AUTH_RATE_LIMIT_WINDOW_SECONDS"),
            "Sign-in rate limit",
            "Limits how fast one address can try to sign in or register.",
            True,
            "{1} attempts per {2} second(s)",
            (s.auth_rate_limit_attempts, s.auth_rate_limit_window_seconds),
        ),
        Option(
            ACCOUNTS,
            ("SESSION_LIFETIME_SECONDS",),
            "Sign-in session lifetime",
            "How long a WebUI sign-in lasts.",
            True,
            *(
                ("{1} day(s)", (s.session_lifetime_seconds // 86400,))
                if s.session_lifetime_seconds % 86400 == 0
                else ("{1} hour(s)", (_hours(s.session_lifetime_seconds),))
            ),
        ),
        Option(
            ACCOUNTS,
            ("PASSWORD_RESET_LIFETIME_MINUTES",),
            "Password reset links",
            "How long a link an administrator issues for a forgotten password works.",
            True,
            "valid for {1} minute(s)",
            (s.password_reset_lifetime_minutes,),
        ),
        Option(
            ACCOUNTS,
            ("SECURE_COOKIES",),
            "Secure cookies",
            "Browsers send the sign-in cookie only over HTTPS. Turn it on when the WebUI is served over HTTPS.",
            s.secure_cookies,
        ),
        Option(
            ACCOUNTS,
            ("SECRET_KEY",),
            "Secret key changed from the placeholder",
            "Signs the server's tokens. Set your own long random value.",
            s.secret_key != "change-me" and len(s.secret_key) >= 16,
        ),
    ]


def _network(s: Settings) -> list[Option]:
    networks = len(s.webui_allowed_network_list)
    proxies = len(s.trusted_proxy_list)
    origins = len(s.cors_origin_list)
    return [
        Option(
            NETWORK,
            ("WEBUI_ALLOWED_NETWORKS",),
            "WebUI network allow-list",
            "Only these networks can open the WebUI and the management API. RustDesk clients are never restricted.",
            networks > 0,
            "{1} network(s)" if networks else None,
            (networks,) if networks else (),
        ),
        Option(
            NETWORK,
            ("TRUSTED_PROXIES",),
            "Trusted reverse proxies",
            "Addresses of proxies whose forwarded client address is believed.",
            proxies > 0,
            "{1} address(es)" if proxies else None,
            (proxies,) if proxies else (),
        ),
        Option(
            NETWORK,
            ("CORS_ORIGINS",),
            "Cross-origin (CORS) access",
            "Other websites that may call the API from a browser. Empty allows none.",
            origins > 0,
            "{1} origin(s)" if origins else None,
            (origins,) if origins else (),
        ),
        Option(
            NETWORK,
            ("SSL_CERTFILE", "SSL_KEYFILE"),
            "HTTPS served by this server",
            "The server itself answers over HTTPS. Not needed behind a reverse proxy that does it.",
            bool(s.ssl_certfile and s.ssl_keyfile),
        ),
        Option(
            NETWORK,
            ("EXTERNAL_URL",),
            "External URL",
            "The address people and clients use to reach this server.",
            True,
            "{1}",
            (s.external_url,),
        ),
        Option(
            NETWORK,
            ("RUSTDESK_ID_SERVER", "RUSTDESK_RELAY_SERVER"),
            "RustDesk ID server",
            "Offered on the Deploy page and in the installer, so clients need no typing.",
            bool(s.rustdesk_id_server),
            "{1}" if s.rustdesk_id_server else None,
            (s.rustdesk_id_server,) if s.rustdesk_id_server else (),
        ),
        Option(
            NETWORK,
            ("RUSTDESK_KEY",),
            "RustDesk public key",
            "The key of your ID server, offered together with its address.",
            bool(s.rustdesk_key),
        ),
        Option(
            NETWORK,
            ("API_DOCS_ENABLED",),
            "API documentation (/docs)",
            "Interactive documentation of the API. Turn it off in production if you do not need it.",
            s.api_docs_enabled,
        ),
        Option(
            NETWORK,
            ("METRICS_TOKEN",),
            "Prometheus metrics (/metrics)",
            "Metrics for a monitoring system, protected by this token. Without one the page is off.",
            bool(s.metrics_token),
        ),
    ]


def _web_client_option(s: Settings) -> Option:
    help_text = (
        "Control a device from the browser: Open in browser on a device. The server bridges the browser to "
        "hbbs and hbbr, which must be reachable from here (WebSocket ports 21118 and 21119)."
    )
    env = ("WEB_CLIENT_ENABLED", "WEB_CLIENT_HBBS_URL", "WEB_CLIENT_HBBR_URL", "WEB_CLIENT_MAX_SESSIONS")
    label = "Web client"
    if not s.web_client_enabled:
        return Option(DEVICES, env, label, help_text, False)
    if webclient_service.problems(s):
        # Which variable is missing is on the Deploy page for administrators; the report only says it is not usable yet.
        return Option(
            DEVICES, env, label, help_text, False, "switched on, but RUSTDESK_KEY or an ID server is missing"
        )
    return Option(
        DEVICES,
        env,
        label,
        help_text,
        True,
        "up to {1} sessions at once, through {2} and {3}",
        (
            s.web_client_max_sessions,
            urlsplit(webclient_service.hbbs_url(s)).hostname or "",
            urlsplit(webclient_service.hbbr_url(s)).hostname or "",
        ),
    )


def _devices(s: Settings) -> list[Option]:
    return [
        Option(
            DEVICES,
            ("DEVICE_ONLINE_TIMEOUT",),
            "Online timeout",
            "A device that has not sent a heartbeat for this long counts as offline.",
            True,
            "{1} second(s)",
            (s.device_online_timeout,),
        ),
        _web_client_option(s),
        Option(
            DEVICES,
            ("DEVICE_UUID_REBIND",),
            "Protection against device take-over",
            "What happens when a known ID shows up with a different identity: approve waits for an owner, deny ignores it, allow adopts it.",
            s.device_uuid_rebind != "allow",
            "mode: {1}",
            (s.device_uuid_rebind,),
        ),
        Option(
            DEVICES,
            ("NEW_DEVICE_POLICY", "NEW_DEVICE_PENDING_LIMIT"),
            "Approval of new devices",
            "With approve, a device this server has not seen before waits on the Devices page for an administrator: it gets no policy and cannot be opened in the browser until approved.",
            s.new_device_policy == "approve",
            "mode: {1}",
            (s.new_device_policy,),
        ),
        Option(
            DEVICES,
            ("ALLOW_SYSINFO_PRESETS",),
            "Presets in a new device's first upload",
            "Clients built with preset options may place a new device in a user's address book or group.",
            s.allow_sysinfo_presets,
        ),
        Option(
            DEVICES,
            ("ADDRESS_BOOK_LEGACY_MODE",),
            "Old address book only",
            "Every client keeps the old whole-list address book instead of the newer personal and shared books.",
            s.address_book_legacy_mode,
        ),
        Option(
            DEVICES,
            ("DEVICE_STALE_DAYS",),
            "Archive silent devices",
            "Devices unseen for this long are hidden from the list and counts until they report again.",
            s.device_stale_days > 0,
            "after {1} day(s)" if s.device_stale_days > 0 else None,
            (s.device_stale_days,) if s.device_stale_days > 0 else (),
        ),
        Option(
            DEVICES,
            ("MIN_CLIENT_VERSION",),
            "Flag outdated clients",
            "Clients older than this version are marked on the device list.",
            bool(s.min_client_version),
            "older than {1}" if s.min_client_version else None,
            (s.min_client_version,) if s.min_client_version else (),
        ),
        Option(
            DEVICES,
            ("CLIENT_AUDIT_RATE_LIMIT_PER_MINUTE",),
            "Rate limit for client reports",
            "How many connection reports one address may send per minute.",
            True,
            "{1} per minute",
            (s.client_audit_rate_limit_per_minute,),
        ),
    ]


def _data(s: Settings) -> list[Option]:
    keys = _count(s.data_encryption_key)
    scheme = urlsplit(s.database_url).scheme.split("+")[0]
    backups = s.backup_interval_hours > 0
    return [
        Option(
            DATA,
            ("DATABASE_URL",),
            "Database",
            "Where everything is stored. SQLite is the default.",
            True,
            "{1}",
            ("SQLite" if scheme == "sqlite" else scheme,),
        ),
        Option(
            DATA,
            ("DATA_ENCRYPTION_KEY",),
            "Encryption of stored secrets",
            "Lets the server keep secrets it must read back, such as a shared address book's passwords.",
            keys > 0,
            "{1} key(s)" if keys else None,
            (keys,) if keys else (),
        ),
        Option(
            DATA,
            ("BACKUP_INTERVAL_HOURS", "BACKUP_KEEP", "BACKUP_DIR"),
            "Scheduled backups",
            "A copy of the database is written regularly, and the newest ones are kept.",
            backups,
            "every {1} hour(s), keeping {2}" if backups else None,
            (s.backup_interval_hours, s.backup_keep) if backups else (),
        ),
        Option(
            DATA,
            ("BACKUP_BEFORE_MIGRATION",),
            "Backup before an upgrade",
            "A copy is taken before the server changes the database to a newer layout.",
            s.backup_before_migration,
        ),
        Option(
            DATA,
            ("AUDIT_LOG_RETENTION_DAYS", "LOG_RETENTION_INTERVAL_HOURS"),
            "Activity log retention",
            "Older entries of the activity log are deleted. Off keeps them forever.",
            s.audit_log_retention_days > 0,
            "keeps {1} day(s)" if s.audit_log_retention_days > 0 else None,
            (s.audit_log_retention_days,) if s.audit_log_retention_days > 0 else (),
        ),
        Option(
            DATA,
            ("CONNECTION_LOG_RETENTION_DAYS",),
            "Connection log retention",
            "Older entries of the connection log are deleted. Off keeps them forever.",
            s.connection_log_retention_days > 0,
            "keeps {1} day(s)" if s.connection_log_retention_days > 0 else None,
            (s.connection_log_retention_days,) if s.connection_log_retention_days > 0 else (),
        ),
        Option(
            DATA,
            ("IP_LOOKUP_ENABLED", "IP_LOOKUP_TIMEOUT_SECONDS", "IP_LOOKUP_MAX_PER_MINUTE"),
            "IP address lookups",
            "Owner details of a public IP address are fetched from the internet registries. Turn off to make no outbound lookups.",
            s.ip_lookup_enabled,
        ),
        Option(
            DATA,
            ("UPDATE_CHECK_ENABLED",),
            "Check for a newer version",
            "The Dashboard asks GitHub, at most every few hours and sending nothing about this installation, whether a newer build of the project exists. Turn off to make no such request.",
            s.update_check_enabled,
        ),
    ]


def _notifications(s: Settings) -> list[Option]:
    channels = notification_service.configured_channels(s)
    targets = notification_service.describe_targets(s)
    events = len(s.notify_event_list)
    return [
        Option(
            NOTIFICATIONS,
            ("NOTIFY_WEBHOOK_URL", "NOTIFY_WEBHOOK_SECRET"),
            "Webhook",
            "A JSON post to Slack, Mattermost, Discord or an automation tool.",
            "webhook" in channels,
            "to {1}" if "webhook" in channels else None,
            (targets["webhook"],) if "webhook" in channels else (),
        ),
        Option(
            NOTIFICATIONS,
            ("NOTIFY_NTFY_URL", "NOTIFY_NTFY_TOKEN"),
            "ntfy",
            "Phone notifications through ntfy.sh or your own ntfy server.",
            "ntfy" in channels,
            "to {1}" if "ntfy" in channels else None,
            (targets["ntfy"],) if "ntfy" in channels else (),
        ),
        Option(
            NOTIFICATIONS,
            (
                "NOTIFY_SMTP_HOST",
                "NOTIFY_SMTP_PORT",
                "NOTIFY_SMTP_USER",
                "NOTIFY_SMTP_PASSWORD",
                "NOTIFY_SMTP_SECURITY",
                "NOTIFY_EMAIL_FROM",
                "NOTIFY_EMAIL_TO",
            ),
            "E-mail",
            "Mail through your own SMTP server.",
            "email" in channels,
            "{1}" if "email" in channels else None,
            (targets["email"],) if "email" in channels else (),
        ),
        Option(
            NOTIFICATIONS,
            (
                "NOTIFY_EVENTS",
                "NOTIFY_OFFLINE_AFTER_MINUTES",
                "NOTIFY_MAX_PER_MINUTE",
                "NOTIFY_TIMEOUT_SECONDS",
            ),
            "Events that are sent",
            "Which events are reported, when a watched device counts as offline, and how many messages may go out per minute.",
            bool(channels) and events > 0,
            "{1} kind(s) of event, at most {2} per minute" if channels and events else None,
            (events, s.notify_max_per_minute) if channels and events else (),
        ),
    ]


def _certificate_option(s: Settings) -> Option:
    tool = signing_service.find_tool(s) is not None
    storage = bool(s.data_encryption_key)
    host = signing_service.timestamp_host(s)
    if not tool:
        detail, args = "osslsigncode was not found", ()
    elif not storage:
        detail, args = "DATA_ENCRYPTION_KEY is not set", ()
    elif host:
        detail, args = "timestamped by {1}", (host,)
    else:
        detail, args = "signatures are not timestamped", ()
    return Option(
        INSTALLER,
        ("INSTALLER_OSSLSIGNCODE", "INSTALLER_TIMESTAMP_URL"),
        "Signing with an uploaded certificate",
        "A certificate uploaded on the Deploy page signs the installers built here. Needs osslsigncode and DATA_ENCRYPTION_KEY.",
        tool and storage,
        detail,
        args,
    )


def _installer(s: Settings) -> list[Option]:
    reason = installer_service.availability(s)
    return [
        Option(
            INSTALLER,
            ("INSTALLER_BUILD_ENABLED", "INSTALLER_MAKENSIS"),
            "Build installers on the server",
            "The Deploy page can build a Windows setup file here. Needs NSIS (makensis) on the server.",
            reason is None,
            {"no_makensis": "NSIS (makensis) was not found"}.get(reason or ""),
        ),
        Option(
            INSTALLER,
            ("INSTALLER_SIGN_COMMAND",),
            "Signing of built installers",
            "A command that signs each setup file the server builds. Its text is never shown.",
            bool(s.installer_sign_command),
        ),
        _certificate_option(s),
        Option(
            INSTALLER,
            ("INSTALLER_DIR",),
            "Own folder for installer files",
            "Where downloaded and built installer files are kept. Off means next to the database.",
            bool(s.installer_dir),
        ),
        Option(
            INSTALLER,
            ("INSTALLER_ICON",),
            "Own icon for the installer",
            "An .ico file for the setup file. Off uses the RustDesk icon.",
            bool(s.installer_icon),
        ),
    ]


def _server(s: Settings) -> list[Option]:
    return [
        Option(
            SERVER,
            ("RUSTDESK_API_HOST", "RUSTDESK_API_PORT"),
            "Listening address",
            "The address and port this server listens on.",
            True,
            "{1}:{2}",
            (s.rustdesk_api_host, s.rustdesk_api_port),
        ),
        Option(
            SERVER,
            ("SERVER_METRICS_INTERVAL_SECONDS", "SERVER_METRICS_HISTORY_MINUTES"),
            "CPU and memory charts",
            "The Dashboard's server charts: how often they are sampled and how far back they go.",
            True,
            "every {1} second(s), the last {2} minute(s)",
            (s.server_metrics_interval_seconds, s.server_metrics_history_minutes),
        ),
        Option(
            SERVER,
            ("LOG_LEVEL",),
            "Log level",
            "How much the server writes to its log.",
            True,
            "{1}",
            (s.log_level.upper(),),
        ),
    ]


def report(settings: Settings) -> list[Option]:
    options = [
        *_accounts(settings),
        *_network(settings),
        *_devices(settings),
        *_data(settings),
        *_notifications(settings),
        *_installer(settings),
        *_server(settings),
    ]
    order = {name: i for i, name in enumerate(GROUPS)}
    return sorted(options, key=lambda o: order[o.group])  # stable: keeps the order above


def covered_variables() -> set[str]:
    """Every environment variable a report mentions (used by a test to keep the list complete)."""
    return {name for option in report(Settings.model_construct()) for name in option.env} | NOT_LISTED
