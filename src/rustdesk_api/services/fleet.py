"""Fleet housekeeping that runs in the background: telling an administrator that a
watched device went quiet, and archiving devices that have been gone for good.

Both are idempotent and safe to run in several server processes at once (worst case,
one outage is announced twice)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rustdesk_api.clientversion import is_older
from rustdesk_api.config import Settings
from rustdesk_api.models.device import Device
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import notifications


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _aware(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def _label(device: Device) -> str:
    name = device.alias or device.hostname
    return f"{device.rustdesk_id} ({name})" if name else device.rustdesk_id


@dataclass(frozen=True)
class WatchResult:
    went_offline: int = 0
    came_back: int = 0


def check_watched_devices(
    db: Session, settings: Settings, *, now: datetime.datetime | None = None
) -> WatchResult:
    """Announces devices flagged `watch_offline` that have been silent for
    NOTIFY_OFFLINE_AFTER_MINUTES (never sooner than the online timeout), once per
    outage, and - if `device_back_online` is enabled - when they report again."""
    now = now or _utcnow()
    silent_for = max(settings.notify_offline_after_minutes * 60, settings.device_online_timeout)
    cutoff = now - datetime.timedelta(seconds=silent_for)

    went_offline = came_back = 0
    watched = select(Device).where(Device.watch_offline.is_(True), Device.last_seen.is_not(None))
    for device in db.execute(watched).scalars():
        last_seen = _aware(device.last_seen)  # type: ignore[arg-type]
        if device.offline_notified_at is None:
            if last_seen < cutoff:
                minutes = int((now - last_seen).total_seconds() // 60)
                device.offline_notified_at = now
                notifications.dispatch(
                    "device_offline",
                    "Device offline",
                    f"Device {_label(device)} has not reported for {minutes} minute(s).",
                    data={"rustdesk_id": device.rustdesk_id, "last_seen": last_seen.isoformat()},
                )
                went_offline += 1
        elif last_seen > _aware(device.offline_notified_at):
            device.offline_notified_at = None
            notifications.dispatch(
                "device_back_online",
                "Device back online",
                f"Device {_label(device)} is reporting again.",
                data={"rustdesk_id": device.rustdesk_id},
            )
            came_back += 1
    db.commit()
    return WatchResult(went_offline, came_back)


def archive_stale(db: Session, settings: Settings, *, now: datetime.datetime | None = None) -> int:
    """Archives devices unseen for DEVICE_STALE_DAYS (a device that never reported is
    measured from when it was created). Devices flagged for offline alerts are kept:
    someone is watching them on purpose. One audit entry per run that archived any."""
    if settings.device_stale_days <= 0:
        return 0
    now = now or _utcnow()
    cutoff = now - datetime.timedelta(days=settings.device_stale_days)
    stale = db.execute(
        select(Device).where(
            Device.archived_at.is_(None),
            Device.watch_offline.is_(False),
            func.coalesce(Device.last_seen, Device.created_at) < cutoff,
        )
    ).scalars()
    archived = list(stale)
    for device in archived:
        device.archived_at = now
        device_service.record_event(db, device, "archived", {"stale_days": settings.device_stale_days})
    if archived:
        audit_service.record(
            db,
            action="devices_archived",
            detail={
                "count": len(archived),
                "stale_days": settings.device_stale_days,
                "rustdesk_ids": [d.rustdesk_id for d in archived[:20]],
            },
        )
    db.commit()
    return len(archived)


def set_archived(db: Session, device: Device, archived: bool) -> None:
    """A manual (un)archive from the WebUI."""
    if archived and device.archived_at is None:
        device.archived_at = _utcnow()
        device.watch_offline = False
        device.offline_notified_at = None
        device_service.record_event(db, device, "archived", {"manual": True})
    elif not archived and device.archived_at is not None:
        device.archived_at = None
        device_service.record_event(db, device, "unarchived", {"manual": True})


def set_watch(device: Device, watch: bool) -> None:
    device.watch_offline = watch
    if not watch:
        device.offline_notified_at = None


def count_outdated(db: Session, settings: Settings) -> int:
    """Devices reporting a client older than MIN_CLIENT_VERSION. Versions are compared
    in Python (they are strings like "1.4.0-1"), over the distinct values only."""
    if not settings.min_client_version:
        return 0
    rows = db.execute(
        select(Device.client_version, func.count(Device.id))
        .where(Device.archived_at.is_(None), Device.client_version.is_not(None))
        .group_by(Device.client_version)
    ).all()
    return sum(count for version, count in rows if is_older(version, settings.min_client_version))
