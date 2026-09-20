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

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    rustdesk_id_server: str = Field(default="", alias="RUSTDESK_ID_SERVER")
    rustdesk_relay_server: str = Field(default="", alias="RUSTDESK_RELAY_SERVER")
    rustdesk_key: str = Field(default="", alias="RUSTDESK_KEY")

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

    ssl_certfile: str = Field(default="", alias="SSL_CERTFILE")
    ssl_keyfile: str = Field(default="", alias="SSL_KEYFILE")

    @field_validator("data_encryption_key")
    @classmethod
    def _check_data_encryption_key(cls, value: str) -> str:
        from rustdesk_api.security.encryption import validate_keys

        validate_keys(value)
        return value.strip()

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
