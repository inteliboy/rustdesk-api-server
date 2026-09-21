"""Central application configuration.

All configuration must flow through the :class:`Settings` object returned by
:func:`get_settings`. Do not read `os.environ` directly elsewhere in the
application; inject settings instead, so tests can override them cleanly.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from rustdesk_api.clientversion import parse_version

# What NOTIFY_EVENTS may name (see services/notifications.py for when each is sent).
NOTIFY_EVENT_NAMES = (
    "device_offline",
    "device_back_online",
    "new_device",
    "device_takeover_attempt",
    "client_alarm",
    "account_locked",
    "backup_failed",
)


# Characters INSTALLER_SIGN_COMMAND may not contain (it is written into an NSIS script).
FORBIDDEN_SIGN_CHARS = tuple(chr(code) for code in (0x60, 0x0A, 0x0D, 0x00))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # A rejected value (e.g. a mistyped DATA_ENCRYPTION_KEY) must not be
        # echoed back in the startup error.
        hide_input_in_errors=True,
    )

    rustdesk_api_host: str = Field(default="0.0.0.0", alias="RUSTDESK_API_HOST")
    rustdesk_api_port: int = Field(default=21114, alias="RUSTDESK_API_PORT")

    database_url: str = Field(default="sqlite:///./data/rustdesk.db", alias="DATABASE_URL")

    secret_key: str = Field(default="change-me", alias="SECRET_KEY")

    # Encrypts the secrets this server must read back (today: a shared address
    # book's connection passwords). Separate from SECRET_KEY on purpose. Empty
    # means such secrets are accepted but not stored. Comma-separated for key
    # rotation: the first key encrypts, all of them decrypt.
    data_encryption_key: str = Field(default="", alias="DATA_ENCRYPTION_KEY")

    allow_registration: bool = Field(default=False, alias="ALLOW_REGISTRATION")

    device_online_timeout: int = Field(default=120, alias="DEVICE_ONLINE_TIMEOUT")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    api_docs_enabled: bool = Field(default=True, alias="API_DOCS_ENABLED")

    cors_origins: str = Field(default="", alias="CORS_ORIGINS")

    trusted_proxies: str = Field(default="", alias="TRUSTED_PROXIES")

    external_url: str = Field(default="http://127.0.0.1:21114", alias="EXTERNAL_URL")

    # Dashboard: whether this is the latest build of the project. Asks GitHub (anonymously, at most
    # every few hours, sending nothing about this installation); false makes no request at all.
    update_check_enabled: bool = Field(default=True, alias="UPDATE_CHECK_ENABLED")

    # The commit this build was made from. Set by the Docker image (a build
    # argument); when empty the server asks the git checkout it runs from.
    git_commit: str = Field(default="", alias="GIT_COMMIT")

    rustdesk_id_server: str = Field(default="", alias="RUSTDESK_ID_SERVER")
    rustdesk_relay_server: str = Field(default="", alias="RUSTDESK_RELAY_SERVER")
    rustdesk_key: str = Field(default="", alias="RUSTDESK_KEY")

    # The web client: "Open in browser" on a device. Browsers cannot speak the
    # RustDesk TCP protocol, but hbbs and hbbr also listen on WebSocket ports
    # (21118 and 21119), so this server bridges the browser to them - only for
    # a signed-in user who may control that one device. Off by default: it needs
    # RUSTDESK_ID_SERVER and RUSTDESK_KEY, and hbbs/hbbr reachable from here.
    web_client_enabled: bool = Field(default=False, alias="WEB_CLIENT_ENABLED")
    # Where the bridge dials. Empty = ws://<host of RUSTDESK_ID_SERVER>:21118 and
    # ws://<host of RUSTDESK_RELAY_SERVER, else the ID server>:21119.
    web_client_hbbs_url: str = Field(default="", alias="WEB_CLIENT_HBBS_URL")
    web_client_hbbr_url: str = Field(default="", alias="WEB_CLIENT_HBBR_URL")
    # Browser sessions open at the same time (each holds a relay connection).
    web_client_max_sessions: int = Field(default=10, ge=1, le=200, alias="WEB_CLIENT_MAX_SESSIONS")

    session_lifetime_seconds: int = Field(default=604800, alias="SESSION_LIFETIME_SECONDS")

    auth_rate_limit_attempts: int = Field(default=10, alias="AUTH_RATE_LIMIT_ATTEMPTS")
    auth_rate_limit_window_seconds: int = Field(default=60, alias="AUTH_RATE_LIMIT_WINDOW_SECONDS")

    # Per client IP, per minute, for the unauthenticated /api/audit/* client
    # endpoints. Much looser than the login limiter: a busy site legitimately
    # posts several events per session, and the client retries with backoff
    # on 429, so this only caps abuse rather than normal traffic.
    client_audit_rate_limit_per_minute: int = Field(default=300, alias="CLIENT_AUDIT_RATE_LIMIT_PER_MINUTE")

    # WHOIS-style details (RDAP) for public IPs shown in the WebUI. The server
    # queries rdap.org / the regional registries with the public IP only (never
    # private ones); set to false to make no outbound lookups at all.
    ip_lookup_enabled: bool = Field(default=True, alias="IP_LOOKUP_ENABLED")
    ip_lookup_timeout_seconds: float = Field(default=4.0, alias="IP_LOOKUP_TIMEOUT_SECONDS")
    # Cap on uncached lookups per minute, across all users of this process.
    ip_lookup_max_per_minute: int = Field(default=30, alias="IP_LOOKUP_MAX_PER_MINUTE")

    # The RustDesk client picks its address-book mode from the server: the
    # newer per-item protocol (personal + shared books) when /api/ab/personal
    # returns a guid, the legacy whole-list one (GET/POST /api/ab) on a 404.
    # true makes this server answer 404 so every client stays on the legacy
    # book - a fallback for a client the newer protocol misbehaves with. Both
    # modes use the same personal book, so nothing is lost by switching.
    address_book_legacy_mode: bool = Field(default=False, alias="ADDRESS_BOOK_LEGACY_MODE")

    # Log retention: rows older than this many days are deleted by a background
    # task (and by `rustdesk-api purge-logs`). 0 keeps that kind of log forever.
    # The audit log is the WebUI's activity feed (logins, admin changes); the
    # connection log covers both the session and the file-transfer logs the
    # RustDesk client reports. Expired login sessions are always cleaned up.
    audit_log_retention_days: int = Field(default=365, ge=0, alias="AUDIT_LOG_RETENTION_DAYS")
    connection_log_retention_days: int = Field(default=180, ge=0, alias="CONNECTION_LOG_RETENTION_DAYS")
    # How often the background task runs (it also runs once at startup). Each
    # run that deletes something adds one audit entry, hence the daily default.
    log_retention_interval_hours: int = Field(default=24, ge=1, alias="LOG_RETENTION_INTERVAL_HOURS")

    # Database backups (SQLite only). The server writes a consistent snapshot every
    # BACKUP_INTERVAL_HOURS and keeps the newest BACKUP_KEEP of them (0 hours turns the
    # schedule off), and takes one before it applies a pending migration, so a bad
    # upgrade can be undone. Backups are whole copies of the database - password
    # hashes included - so keep the directory as private as the database itself.
    # Empty BACKUP_DIR means a `backups` folder next to the database file.
    backup_dir: str = Field(default="", alias="BACKUP_DIR")
    backup_interval_hours: int = Field(default=24, ge=0, alias="BACKUP_INTERVAL_HOURS")
    backup_keep: int = Field(default=7, ge=1, le=1000, alias="BACKUP_KEEP")
    backup_before_migration: bool = Field(default=True, alias="BACKUP_BEFORE_MIGRATION")

    # Notifications: any of the three channels below may be set, all that are set
    # receive every enabled event. The values are secrets (a webhook URL or ntfy
    # topic is a credential), so they live here and never in the database, and are
    # never shown in the WebUI or the logs.
    notify_webhook_url: str = Field(default="", alias="NOTIFY_WEBHOOK_URL", repr=False)
    # Signs each webhook body (HMAC-SHA256, header X-RustDesk-API-Signature).
    notify_webhook_secret: str = Field(default="", alias="NOTIFY_WEBHOOK_SECRET", repr=False)
    # The full topic URL, e.g. https://ntfy.sh/my-private-topic or your own server.
    notify_ntfy_url: str = Field(default="", alias="NOTIFY_NTFY_URL", repr=False)
    notify_ntfy_token: str = Field(default="", alias="NOTIFY_NTFY_TOKEN", repr=False)
    notify_smtp_host: str = Field(default="", alias="NOTIFY_SMTP_HOST")
    notify_smtp_port: int = Field(default=587, ge=1, le=65535, alias="NOTIFY_SMTP_PORT")
    notify_smtp_user: str = Field(default="", alias="NOTIFY_SMTP_USER")
    notify_smtp_password: str = Field(default="", alias="NOTIFY_SMTP_PASSWORD", repr=False)
    # starttls (port 587), ssl (port 465) or none (a trusted local relay only).
    notify_smtp_security: str = Field(default="starttls", alias="NOTIFY_SMTP_SECURITY")
    notify_email_from: str = Field(default="", alias="NOTIFY_EMAIL_FROM")
    notify_email_to: str = Field(default="", alias="NOTIFY_EMAIL_TO")
    # Which events are sent (comma-separated; see services/notifications.EVENTS).
    notify_events: str = Field(
        default="device_offline,new_device,device_takeover_attempt,client_alarm,account_locked,backup_failed",
        alias="NOTIFY_EVENTS",
    )
    # A device flagged "notify when offline" is reported after this long without a heartbeat.
    notify_offline_after_minutes: int = Field(
        default=10, ge=1, le=10080, alias="NOTIFY_OFFLINE_AFTER_MINUTES"
    )
    # At most this many notifications per minute across all events (a bulk import or an
    # alarm storm must not flood a phone); the rest are dropped and counted.
    notify_max_per_minute: int = Field(default=20, ge=1, le=600, alias="NOTIFY_MAX_PER_MINUTE")
    notify_timeout_seconds: float = Field(default=10.0, gt=0, le=60, alias="NOTIFY_TIMEOUT_SECONDS")

    # Fleet hygiene. A device unseen for this many days is archived: hidden from the
    # default device list and the counts, back the moment it reports again. 0 = never.
    # Devices flagged "notify when offline" are never archived. A device still waiting
    # for approval after this long silent is forgotten instead (NEW_DEVICE_POLICY).
    device_stale_days: int = Field(default=0, ge=0, alias="DEVICE_STALE_DAYS")
    # Clients older than this (e.g. 1.4.0) are flagged as outdated. Empty turns it off.
    min_client_version: str = Field(default="", alias="MIN_CLIENT_VERSION")

    # Windows installer builder (Deploy page). The server downloads a RustDesk MSI from
    # GitHub and wraps it with NSIS (makensis) into a setup .exe that installs it and
    # applies this server's settings. It needs makensis on the server (the Docker image has
    # it; on Windows install NSIS) and outbound HTTPS to github.com. Built files are kept
    # under INSTALLER_DIR (empty = an `installers` folder next to the database).
    installer_build_enabled: bool = Field(default=True, alias="INSTALLER_BUILD_ENABLED")
    installer_makensis: str = Field(default="", alias="INSTALLER_MAKENSIS")
    installer_dir: str = Field(default="", alias="INSTALLER_DIR")
    # Optional code signing of the finished .exe: a command that NSIS runs with %1 replaced by
    # the file, e.g. signtool sign /sha1 <thumbprint> /fd SHA256 /tr <url> /td SHA256 "%1"
    # (a certificate in the server's store) or osslsigncode. Do not put a password in it.
    installer_sign_command: str = Field(default="", alias="INSTALLER_SIGN_COMMAND", repr=False)
    # A .ico for the setup file; empty = the RustDesk icon, fetched from GitHub.
    installer_icon: str = Field(default="", alias="INSTALLER_ICON")
    # Signing with a certificate an administrator uploads on the Deploy page (stored encrypted
    # with DATA_ENCRYPTION_KEY). It is done with osslsigncode, which must be on the server (the
    # Docker image has it); empty INSTALLER_OSSLSIGNCODE = the one on PATH. The signature is
    # timestamped by INSTALLER_TIMESTAMP_URL so it stays valid after the certificate expires;
    # empty = no timestamp (and no outbound request).
    installer_osslsigncode: str = Field(default="", alias="INSTALLER_OSSLSIGNCODE")
    installer_timestamp_url: str = Field(
        default="http://timestamp.digicert.com", alias="INSTALLER_TIMESTAMP_URL"
    )

    # Server tab: this process's CPU and memory, sampled every N seconds into a
    # rolling in-memory window (nothing is written to the database, and the
    # history starts empty after a restart). With several worker processes each
    # one keeps its own.
    server_metrics_interval_seconds: int = Field(
        default=10, ge=5, le=300, alias="SERVER_METRICS_INTERVAL_SECONDS"
    )
    server_metrics_history_minutes: int = Field(
        default=60, ge=5, le=360, alias="SERVER_METRICS_HISTORY_MINUTES"
    )

    # Clients built with `preset-*` options (address book, group, user, strategy
    # ...) put them in their sysinfo upload, which carries no credentials. Off
    # by default because honouring them lets anyone who can reach this server
    # place a *new* device in a named user's address book or group. Only a
    # device's first registration is ever affected. `rustdesk --assign` (which
    # is authenticated) works regardless.
    allow_sysinfo_presets: bool = Field(default=False, alias="ALLOW_SYSINFO_PRESETS")

    # A new device id with an unknown `uuid` may register itself, but a *known*
    # id arriving with a different uuid in an unauthenticated upload is how a
    # stranger who guesses a 9-digit id would take that device over (the uuid is
    # what ties heartbeats, policy and disconnects to the real client).
    #   approve - park the new uuid; the owner/an administrator accepts it (default)
    #   deny    - ignore such uploads
    #   allow   - adopt it (the behaviour before this option existed)
    # An upload that carries a valid login token of the owner or an
    # administrator is trusted in every mode.
    device_uuid_rebind: str = Field(default="approve", alias="DEVICE_UUID_REBIND")

    # What happens to a device id this server has never seen.
    #   allow   - it registers and is managed at once (the behaviour before this option)
    #   approve - it is recorded as "pending": the server keeps its details so an
    #             administrator can recognise it, but sends it no policy and lets nobody
    #             open it in the browser until it is approved on the Devices page.
    # A device an administrator brings in (a sysinfo upload or a client sign-in that
    # carries an administrator's login) is approved at once. Devices already known when
    # this is turned on stay approved.
    new_device_policy: str = Field(default="allow", alias="NEW_DEVICE_POLICY")
    # At most this many devices may wait for approval; further unknown ids are not
    # recorded (an unauthenticated upload must not be able to fill the database).
    new_device_pending_limit: int = Field(default=500, ge=1, le=100000, alias="NEW_DEVICE_PENDING_LIMIT")

    # Self-registration (the /register page and POST /api/v1/auth/register).
    # Only if ALLOW_REGISTRATION is on. With approval on, a new account cannot
    # sign in until an administrator activates it on the Users page.
    registration_requires_approval: bool = Field(default=True, alias="REGISTRATION_REQUIRES_APPROVAL")

    # How long a link an administrator issues for a forgotten password works.
    password_reset_lifetime_minutes: int = Field(
        default=60, ge=5, le=10080, alias="PASSWORD_RESET_LIFETIME_MINUTES"
    )

    # After this many wrong passwords in a row an account is locked for the
    # given time (even the right password is refused meanwhile, or the lock
    # would not stop guessing). 0 turns lockout off.
    login_lockout_threshold: int = Field(default=10, ge=0, alias="LOGIN_LOCKOUT_THRESHOLD")
    login_lockout_minutes: int = Field(default=15, ge=1, le=1440, alias="LOGIN_LOCKOUT_MINUTES")

    # Comma-separated networks (CIDR or single addresses) allowed to reach the
    # WebUI and the management API (/api/v1, the pages, /docs). Empty allows
    # everyone. The RustDesk client endpoints (/api/login, /api/heartbeat, ...)
    # and /health are never restricted: clients connect from anywhere.
    webui_allowed_networks: str = Field(default="", alias="WEBUI_ALLOWED_NETWORKS")

    # Bearer token for GET /metrics (Prometheus). Empty leaves /metrics off.
    metrics_token: str = Field(default="", alias="METRICS_TOKEN")

    secure_cookies: bool = Field(default=False, alias="SECURE_COOKIES")

    # OpenID Connect sign-in (WebUI and the RustDesk client's "Continue with ..."
    # button). Off unless both OIDC_ISSUER and OIDC_CLIENT_ID are set. The provider
    # must be told the redirect URI EXTERNAL_URL + /api/oidc/callback.
    oidc_issuer: str = Field(default="", alias="OIDC_ISSUER")
    oidc_client_id: str = Field(default="", alias="OIDC_CLIENT_ID")
    # Empty for a public client (PKCE is then the only proof of the exchange).
    oidc_client_secret: str = Field(default="", alias="OIDC_CLIENT_SECRET", repr=False)
    # The button's name in the RustDesk client ("Continue with <Name>") and the WebUI.
    oidc_name: str = Field(default="sso", alias="OIDC_NAME")
    oidc_scopes: str = Field(default="openid email profile", alias="OIDC_SCOPES")
    # A provider sign-in with no linked account may create a (non-admin) user. Needs
    # OIDC_ALLOWED_EMAIL_DOMAINS, or anyone the provider will sign in could get one.
    oidc_auto_create_users: bool = Field(default=False, alias="OIDC_AUTO_CREATE_USERS")
    # ... or may be linked to the local user with the same verified e-mail address.
    # Off: control of an address at the provider then cannot take over a local account.
    oidc_link_by_email: bool = Field(default=False, alias="OIDC_LINK_BY_EMAIL")
    # Comma-separated. Limits auto-creation and linking by e-mail to these domains.
    oidc_allowed_email_domains: str = Field(default="", alias="OIDC_ALLOWED_EMAIL_DOMAINS")
    oidc_username_claim: str = Field(default="preferred_username", alias="OIDC_USERNAME_CLAIM")
    oidc_timeout_seconds: float = Field(default=10.0, alias="OIDC_TIMEOUT_SECONDS", gt=0, le=60)

    ssl_certfile: str = Field(default="", alias="SSL_CERTFILE")
    ssl_keyfile: str = Field(default="", alias="SSL_KEYFILE")

    @field_validator("data_encryption_key")
    @classmethod
    def _check_data_encryption_key(cls, value: str) -> str:
        from rustdesk_api.security.encryption import validate_keys

        validate_keys(value)
        return value.strip()

    @field_validator("notify_webhook_url", "notify_ntfy_url")
    @classmethod
    def _check_notify_url(cls, value: str) -> str:
        value = value.strip()
        if value and not value.lower().startswith(("http://", "https://")):
            # The URL may carry a token, so it is not echoed.
            raise ValueError("A notification URL must start with http:// or https://")
        return value

    @field_validator("notify_smtp_security")
    @classmethod
    def _check_smtp_security(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ("starttls", "ssl", "none"):
            raise ValueError("NOTIFY_SMTP_SECURITY must be starttls, ssl or none")
        return value

    @field_validator("notify_events")
    @classmethod
    def _check_notify_events(cls, value: str) -> str:
        names = [n.strip() for n in value.split(",") if n.strip()]
        unknown = [n for n in names if n not in NOTIFY_EVENT_NAMES]
        if unknown:
            raise ValueError(
                f"NOTIFY_EVENTS has unknown event(s) {', '.join(unknown)}; "
                f"known: {', '.join(NOTIFY_EVENT_NAMES)}"
            )
        return ",".join(names)

    @field_validator("web_client_hbbs_url", "web_client_hbbr_url")
    @classmethod
    def _check_web_client_url(cls, value: str) -> str:
        value = value.strip()
        if value and not value.lower().startswith(("ws://", "wss://")):
            raise ValueError("WEB_CLIENT_HBBS_URL and WEB_CLIENT_HBBR_URL must start with ws:// or wss://")
        return value

    @field_validator("installer_timestamp_url")
    @classmethod
    def _check_installer_timestamp_url(cls, value: str) -> str:
        value = value.strip()
        if value and not value.lower().startswith(("http://", "https://")):
            raise ValueError("INSTALLER_TIMESTAMP_URL must start with http:// or https://")
        return value

    @field_validator("installer_sign_command")
    @classmethod
    def _check_installer_sign_command(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if "%1" not in value:
            raise ValueError("INSTALLER_SIGN_COMMAND must contain %1, where NSIS puts the file to sign")
        if any(ch in value for ch in FORBIDDEN_SIGN_CHARS):
            raise ValueError("INSTALLER_SIGN_COMMAND must be one line without backticks")
        return value

    @field_validator("min_client_version")
    @classmethod
    def _check_min_client_version(cls, value: str) -> str:
        value = value.strip()
        if value and parse_version(value) is None:
            raise ValueError("MIN_CLIENT_VERSION must look like 1.4.0")
        return value

    @field_validator("metrics_token")
    @classmethod
    def _check_metrics_token(cls, value: str) -> str:
        value = value.strip()
        if value and len(value) < 16:
            # A guessable scrape token is no protection; the value is not echoed.
            raise ValueError("METRICS_TOKEN must be at least 16 characters (use `rustdesk-api generate-key`)")
        return value

    @field_validator("device_uuid_rebind")
    @classmethod
    def _check_uuid_rebind(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ("approve", "deny", "allow"):
            raise ValueError("DEVICE_UUID_REBIND must be approve, deny or allow")
        return value

    @field_validator("new_device_policy")
    @classmethod
    def _check_new_device_policy(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ("allow", "approve"):
            raise ValueError("NEW_DEVICE_POLICY must be allow or approve")
        return value

    @field_validator("webui_allowed_networks")
    @classmethod
    def _check_allowed_networks(cls, value: str) -> str:
        for part in value.split(","):
            if part.strip():
                try:
                    ipaddress.ip_network(part.strip(), strict=False)
                except ValueError:
                    # The value is a network list, not a secret, so it may be named.
                    raise ValueError(
                        f"WEBUI_ALLOWED_NETWORKS has an invalid entry: {part.strip()!r}"
                    ) from None
        return value.strip()

    @field_validator("oidc_name")
    @classmethod
    def _check_oidc_name(cls, value: str) -> str:
        import re

        value = value.strip()
        # It is sent to the client as `oidc/<name>` and shown as a button label.
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
            raise ValueError("OIDC_NAME must be 1-32 letters, digits, '-' or '_'.")
        return value

    @field_validator("oidc_scopes")
    @classmethod
    def _check_oidc_scopes(cls, value: str) -> str:
        scopes = value.split()
        if "openid" not in scopes:
            raise ValueError("OIDC_SCOPES must include 'openid'.")
        return " ".join(scopes)

    @model_validator(mode="after")
    def _check_oidc(self) -> Settings:
        if not self.oidc_issuer and not self.oidc_client_id:
            return self
        if not self.oidc_issuer or not self.oidc_client_id:
            raise ValueError("Set both OIDC_ISSUER and OIDC_CLIENT_ID, or neither.")
        from rustdesk_api.security.oidc import OidcError, require_https

        try:
            require_https(self.oidc_issuer, "issuer")
            require_https(self.external_url, "external_url")
        except OidcError:
            raise ValueError(
                "OIDC_ISSUER and EXTERNAL_URL must be https:// URLs (http only for localhost)."
            ) from None
        if self.secret_key == "change-me" or len(self.secret_key) < 16:
            # The PKCE verifier and nonce of a sign-in are derived from it.
            raise ValueError("OIDC needs a real SECRET_KEY (at least 16 characters, not 'change-me').")
        if self.oidc_auto_create_users and not self.oidc_allowed_email_domain_list:
            raise ValueError("OIDC_AUTO_CREATE_USERS needs OIDC_ALLOWED_EMAIL_DOMAINS.")
        return self

    @field_validator("database_url")
    @classmethod
    def _resolve_sqlite_path(cls, value: str) -> str:
        """A relative sqlite path is resolved against the current working
        directory the process was started from - the same directory the
        documented quick-start (`cd <repo>` then `python -m rustdesk_api`,
        or Docker's WORKDIR) already establishes. This intentionally does
        NOT resolve relative to the installed package's own location: once
        installed non-editably (e.g. into a Docker image's site-packages),
        that location has nothing to do with where the operator wants data
        stored (CLAUDE.md section 6/49: never assume a fixed filesystem
        layout; resolve paths predictably - "predictable" here means
        "relative to where you run it," not "relative to pip internals")."""
        prefix = "sqlite:///"
        if value.startswith(prefix):
            raw_path = value[len(prefix) :]
            path = Path(raw_path)
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            return f"{prefix}{path.as_posix()}"
        return value

    @property
    def oidc_allowed_email_domain_list(self) -> list[str]:
        return [
            d.strip().lstrip("@").lower() for d in self.oidc_allowed_email_domains.split(",") if d.strip()
        ]

    @property
    def notify_event_list(self) -> list[str]:
        return [n for n in self.notify_events.split(",") if n]

    @property
    def notify_email_recipients(self) -> list[str]:
        return [a.strip() for a in self.notify_email_to.split(",") if a.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [p.strip() for p in self.trusted_proxies.split(",") if p.strip()]

    @property
    def webui_allowed_network_list(self) -> list[IPv4Network | IPv6Network]:
        return [
            ipaddress.ip_network(p.strip(), strict=False)
            for p in self.webui_allowed_networks.split(",")
            if p.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    """Used by tests to force re-reading environment/config."""
    get_settings.cache_clear()
