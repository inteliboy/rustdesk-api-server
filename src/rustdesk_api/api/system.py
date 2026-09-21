"""What an administrator can see and trigger about the server's own housekeeping:
notifications, database backups and fleet hygiene (/api/v1/admin/system, ...).

The settings themselves are environment variables (they hold secrets, and the
server is not to be reconfigured from a browser session), so this is a read-only view
of them plus the two actions that make sense to trigger by hand: send a test
notification and take a backup now."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import enforce_auth_rate_limit, get_current_admin, get_settings_dep, verify_csrf
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.device import Device
from rustdesk_api.models.user import User
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import backup as backup_service
from rustdesk_api.services import notifications as notification_service
from rustdesk_api.services import options as options_service

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class ChannelOut(BaseModel):
    name: str
    configured: bool
    # Where it sends to, without anything secret (a host name, a recipient count).
    target: str | None = None


class EventOut(BaseModel):
    name: str
    description: str
    enabled: bool


class NotificationsOut(BaseModel):
    channels: list[ChannelOut]
    events: list[EventOut]
    offline_after_minutes: int
    max_per_minute: int
    watched_devices: int


class BackupOut(BaseModel):
    name: str
    kind: str
    size: int
    created_at: datetime.datetime


class BackupsOut(BaseModel):
    supported: bool
    directory: str | None
    schedule_hours: int
    keep: int
    before_migration: bool
    items: list[BackupOut]


class FleetOut(BaseModel):
    stale_days: int
    min_client_version: str


class SystemOut(BaseModel):
    notifications: NotificationsOut
    backups: BackupsOut
    fleet: FleetOut


def _notifications(db: Session, settings: Settings) -> NotificationsOut:
    targets = notification_service.describe_targets(settings)
    configured = notification_service.configured_channels(settings)
    enabled = set(settings.notify_event_list)
    return NotificationsOut(
        channels=[
            ChannelOut(name=name, configured=name in configured, target=targets.get(name))
            for name in notification_service.CHANNELS
        ],
        events=[
            EventOut(name=name, description=text, enabled=name in enabled)
            for name, text in notification_service.EVENTS.items()
        ],
        offline_after_minutes=settings.notify_offline_after_minutes,
        max_per_minute=settings.notify_max_per_minute,
        watched_devices=db.execute(
            select(func.count(Device.id)).where(Device.watch_offline.is_(True))
        ).scalar_one(),
    )


def _backups(settings: Settings) -> BackupsOut:
    supported = backup_service.sqlite_path(settings) is not None
    return BackupsOut(
        supported=supported,
        directory=str(backup_service.backup_directory(settings)) if supported else None,
        schedule_hours=settings.backup_interval_hours,
        keep=settings.backup_keep,
        before_migration=settings.backup_before_migration,
        items=[
            BackupOut(name=b.name, kind=b.kind, size=b.size, created_at=b.created_at)
            for b in backup_service.list_backups(settings)
        ],
    )


@router.get("/system", response_model=SystemOut)
def system_overview(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
    settings: Settings = Depends(get_settings_dep),
) -> SystemOut:
    return SystemOut(
        notifications=_notifications(db, settings),
        backups=_backups(settings),
        fleet=FleetOut(stale_days=settings.device_stale_days, min_client_version=settings.min_client_version),
    )


class OptionOut(BaseModel):
    env: list[str]
    label: str
    help: str
    enabled: bool
    # A sentence with {1}, {2} placeholders and the values for them; never a secret.
    detail: str | None
    args: list[str | int]


class OptionGroupOut(BaseModel):
    name: str
    items: list[OptionOut]


@router.get("/options", response_model=list[OptionGroupOut])
def options_overview(
    _admin: User = Depends(get_current_admin), settings: Settings = Depends(get_settings_dep)
) -> list[OptionGroupOut]:
    """Every option that is set with an environment variable, and whether it is on. Secrets are
    reported as set or not, never shown."""
    groups: dict[str, list[OptionOut]] = {}
    for o in options_service.report(settings):
        groups.setdefault(o.group, []).append(
            OptionOut(
                env=list(o.env),
                label=o.label,
                help=o.help,
                enabled=o.enabled,
                detail=o.detail,
                args=list(o.args),
            )
        )
    return [OptionGroupOut(name=name, items=items) for name, items in groups.items()]


class ChannelResult(BaseModel):
    channel: str
    ok: bool
    # A short reason on failure; never the URL or a credential.
    error: str | None = None


@router.post(
    "/notifications/test",
    response_model=list[ChannelResult],
    dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)],
)
def send_test_notification(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
    settings: Settings = Depends(get_settings_dep),
) -> list[ChannelResult]:
    """Sends one test message on every configured channel and says how each went.
    Ignores NOTIFY_EVENTS (it is a test of the channels, not of an event)."""
    if not notification_service.configured_channels(settings):
        raise ApiError(
            "NO_NOTIFICATION_CHANNEL",
            "No notification channel is configured "
            "(see NOTIFY_WEBHOOK_URL, NOTIFY_NTFY_URL and NOTIFY_SMTP_HOST).",
            409,
        )
    note = notification_service.Notification(
        event="test",
        title="Test notification",
        message=f"This is a test from the RustDesk API server, sent by {admin.username}.",
        data={"test": True},
    )
    results = notification_service.deliver(settings, note)
    audit_service.record(
        db,
        action="notification_test",
        actor_id=admin.id,
        result="success" if all(error is None for error in results.values()) else "failure",
        detail={"channels": sorted(results)},
    )
    db.commit()
    return [ChannelResult(channel=c, ok=e is None, error=e) for c, e in results.items()]


@router.post(
    "/backups",
    response_model=BackupOut,
    status_code=201,
    dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)],
)
def create_backup_now(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
    settings: Settings = Depends(get_settings_dep),
) -> BackupOut:
    """A manual snapshot of the database. It is kept until someone deletes it."""
    try:
        path = backup_service.create_backup(settings)
    except backup_service.BackupError as exc:
        raise ApiError("BACKUP_FAILED", str(exc), 409) from exc
    audit_service.record(
        db, action="backup_created", actor_id=admin.id, detail={"name": path.name, "manual": True}
    )
    db.commit()
    created = next((b for b in backup_service.list_backups(settings) if b.name == path.name), None)
    if created is None:  # pragma: no cover - the file was just written
        raise ApiError("BACKUP_FAILED", "The backup was written but could not be read back.", 500)
    return BackupOut(name=created.name, kind=created.kind, size=created.size, created_at=created.created_at)
