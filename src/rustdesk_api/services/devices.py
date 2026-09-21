from __future__ import annotations

import datetime
import hmac
import json

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from rustdesk_api.models.device import Device
from rustdesk_api.models.device_event import DeviceEvent
from rustdesk_api.models.share import DeviceShare
from rustdesk_api.models.tag import Tag
from rustdesk_api.models.user import User
from rustdesk_api.services import audit as audit_service


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _aware(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def record_event(db: Session, device: Device, kind: str, detail: dict | None = None) -> None:
    """Something notable that happened to a device (shown on its timeline)."""
    db.add(DeviceEvent(device_id=device.id, kind=kind, detail=json.dumps(detail) if detail else None))


def same_uuid(known: str | None, reported: str | None) -> bool:
    if known is None or reported is None:
        return False
    return hmac.compare_digest(known.encode(), reported.encode())


def _note_seen(db: Session, device: Device, online_timeout: int | None, now: datetime.datetime) -> None:
    """The device just reported in. If it had been silent for longer than the
    online timeout, that is a "came back online" moment worth remembering (the
    offline part is only known now, so it is recorded with the return)."""
    if online_timeout is None or device.last_seen is None:
        return
    silent_since = _aware(device.last_seen)
    if (now - silent_since).total_seconds() > online_timeout:
        record_event(db, device, "online", {"offline_since": silent_since.isoformat()})


def get_by_rustdesk_id(db: Session, rustdesk_id: str) -> Device | None:
    stmt = select(Device).where(Device.rustdesk_id == rustdesk_id)
    return db.execute(stmt).scalar_one_or_none()


def get_by_rustdesk_ids(db: Session, rustdesk_ids: list[str]) -> dict[str, Device]:
    """One query for many ids (avoids an N+1 when annotating a list)."""
    if not rustdesk_ids:
        return {}
    stmt = select(Device).where(Device.rustdesk_id.in_(rustdesk_ids))
    return {d.rustdesk_id: d for d in db.execute(stmt).scalars()}


def get_by_id(db: Session, device_id: int) -> Device | None:
    return db.get(Device, device_id)


def has_reported_sysinfo(device: Device) -> bool:
    """False for a record that only knows the id (and uuid/address): one created by a
    client login, an import or `--assign`, which no sysinfo upload has ever filled in.
    Any real upload carries at least a hostname, an OS or a version."""
    return any((device.hostname, device.platform, device.client_version))


def register_or_update(
    db: Session,
    *,
    rustdesk_id: str,
    uuid: str | None = None,
    hostname: str | None = None,
    username: str | None = None,
    platform: str | None = None,
    os_version: str | None = None,
    client_version: str | None = None,
    ip_address: str | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    owner_id: int | None = None,
    uuid_policy: str = "allow",
    trusted: bool = False,
    online_timeout: int | None = None,
) -> Device:
    """Idempotent upsert keyed on rustdesk_id (CLAUDE.md section 43: repeated
    sysinfo/heartbeat reports for the same id must not create duplicate
    device rows). Only changed fields are written.

    A known device that reports a *different* `uuid` is not the same install, and
    an unauthenticated upload must not be able to take it over. Unless the
    caller is `trusted` (it proved it is the owner or an administrator) or the
    policy is "allow", nothing is changed: with "approve" the new uuid is parked
    on the device for its owner to accept, with "deny" it is dropped."""
    device = get_by_rustdesk_id(db, rustdesk_id)
    created = device is None
    now = _utcnow()
    if device is None:
        device = Device(rustdesk_id=rustdesk_id, owner_id=owner_id)
        db.add(device)
    else:
        if (
            uuid is not None
            and device.uuid is not None
            and not same_uuid(device.uuid, uuid)
            and not trusted
            and uuid_policy != "allow"
        ):
            if uuid_policy == "approve":
                _park_uuid(db, device, uuid, ip_address)
            return device
        if owner_id is not None and device.owner_id is None:
            # Claim an existing but previously unowned device (e.g. one seen
            # via heartbeat before its owning user ever logged in).
            device.owner_id = owner_id
        _note_seen(db, device, online_timeout, now)

    if uuid is not None and device.uuid is not None and not same_uuid(device.uuid, uuid):
        # Only reachable when trusted (or the policy is "allow").
        record_event(db, device, "uuid_changed", {"trusted": trusted})
    if uuid is not None and device.pending_uuid is not None and same_uuid(device.pending_uuid, uuid):
        _clear_pending(device)

    changed = False
    for field, value in (
        ("uuid", uuid),
        ("hostname", hostname),
        ("username", username),
        ("platform", platform),
        ("os_version", os_version),
        ("client_version", client_version),
        ("ip_address", ip_address),
        ("cpu", cpu),
        ("memory", memory),
    ):
        if value is not None and getattr(device, field) != value:
            setattr(device, field, value)
            changed = True

    device.last_seen = now
    if changed:
        device.updated_at = now

    db.flush()
    if created:
        record_event(db, device, "registered", {"ip": ip_address} if ip_address else None)
    return device


def _clear_pending(device: Device) -> None:
    device.pending_uuid = None
    device.pending_uuid_at = None
    device.pending_uuid_ip = None


def _park_uuid(db: Session, device: Device, uuid: str, ip_address: str | None) -> None:
    first = device.pending_uuid is None
    device.pending_uuid = uuid
    device.pending_uuid_at = _utcnow()
    device.pending_uuid_ip = ip_address
    if first:
        # One entry per request that opens a decision, not one per retry: the
        # client repeats its upload every couple of minutes.
        record_event(db, device, "uuid_change_requested", {"ip": ip_address})
        audit_service.record(
            db,
            action="device_uuid_change_requested",
            target_type="device",
            target_id=device.id,
            result="failure",
            ip_address=ip_address,
            detail={"rustdesk_id": device.rustdesk_id},
        )
    db.flush()


def accept_pending_uuid(db: Session, device: Device) -> bool:
    """Adopt the parked uuid: the machine that sent it is now this device."""
    if device.pending_uuid is None:
        return False
    device.uuid = device.pending_uuid
    record_event(db, device, "uuid_accepted", {"ip": device.pending_uuid_ip})
    _clear_pending(device)
    device.updated_at = _utcnow()
    db.flush()
    return True


def reject_pending_uuid(db: Session, device: Device) -> bool:
    if device.pending_uuid is None:
        return False
    record_event(db, device, "uuid_rejected", {"ip": device.pending_uuid_ip})
    _clear_pending(device)
    db.flush()
    return True


def touch_heartbeat(
    db: Session,
    *,
    rustdesk_id: str,
    ip_address: str | None = None,
    online_timeout: int | None = None,
    api_scheme: str | None = None,
) -> Device | None:
    device = get_by_rustdesk_id(db, rustdesk_id)
    if device is None:
        return None
    now = _utcnow()
    _note_seen(db, device, online_timeout, now)
    device.last_seen = now
    if ip_address and device.ip_address != ip_address:
        device.ip_address = ip_address
    if api_scheme and device.api_scheme != api_scheme:
        device.api_scheme = api_scheme
    db.flush()
    return device


SORT_COLUMNS = {
    "last_seen": Device.last_seen,
    "created": Device.created_at,
    "name": func.lower(func.coalesce(Device.alias, Device.hostname, Device.name)),
    "id": Device.rustdesk_id,
}


def list_devices(
    db: Session,
    *,
    owner_id: int | None,
    shared_with_user_id: int | None = None,
    search: str | None = None,
    group_id: int | None = None,
    tag_id: int | None = None,
    status: str | None = None,
    online_timeout: int = 120,
    sort: str = "last_seen",
    order: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Device], int]:
    """owner_id=None and shared_with_user_id=None means "all devices"
    (admin use only - callers must enforce that). Passing the same user's
    id for both returns everything visible to them: devices they own OR
    devices actively shared with them."""
    stmt = select(Device).options(
        selectinload(Device.owner), selectinload(Device.group), selectinload(Device.tags)
    )
    count_stmt = select(func.count(Device.id))

    visibility = None
    if owner_id is not None:
        visibility = Device.owner_id == owner_id
    if shared_with_user_id is not None:
        shared_device_ids = select(DeviceShare.device_id).where(
            DeviceShare.shared_with_user_id == shared_with_user_id,
            or_(DeviceShare.expires_at.is_(None), DeviceShare.expires_at > _utcnow()),
        )
        shared_condition = Device.id.in_(shared_device_ids)
        visibility = shared_condition if visibility is None else or_(visibility, shared_condition)
    if visibility is not None:
        stmt = stmt.where(visibility)
        count_stmt = count_stmt.where(visibility)

    if group_id is not None:
        stmt = stmt.where(Device.group_id == group_id)
        count_stmt = count_stmt.where(Device.group_id == group_id)

    if tag_id is not None:
        stmt = stmt.where(Device.tags.any(Tag.id == tag_id))
        count_stmt = count_stmt.where(Device.tags.any(Tag.id == tag_id))

    if status in ("online", "offline"):
        cutoff = _utcnow() - datetime.timedelta(seconds=online_timeout)
        # Same rule as Device.is_online: seen within the timeout. A device that
        # never reported (last_seen NULL) is offline.
        online = Device.last_seen.is_not(None) & (Device.last_seen >= cutoff)
        condition = online if status == "online" else ~online
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    if search:
        pattern = f"%{search.strip()}%"
        condition = or_(
            Device.rustdesk_id.ilike(pattern),
            Device.hostname.ilike(pattern),
            Device.alias.ilike(pattern),
            Device.username.ilike(pattern),
            Device.ip_address.ilike(pattern),
            Device.platform.ilike(pattern),
        )
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    total = db.execute(count_stmt).scalar_one()

    page = max(page, 1)
    page_size = max(1, min(page_size, 200))
    column = SORT_COLUMNS.get(sort, Device.last_seen)
    descending = (order == "desc") if order in ("asc", "desc") else sort in ("last_seen", "created")
    ordering = column.desc() if descending else column.asc()
    # NULLs (never seen, no name) go last whichever way it sorts; `id` breaks
    # ties so paging through equal values is stable.
    ordering = ordering.nullslast()
    stmt = stmt.order_by(ordering, Device.id.asc()).offset((page - 1) * page_size).limit(page_size)
    items = list(db.execute(stmt).scalars())
    return items, total


def list_owners_of_shared_devices(db: Session, user_id: int) -> list[User]:
    """Users who own a device that is currently shared with `user_id`."""
    shared_device_ids = select(DeviceShare.device_id).where(
        DeviceShare.shared_with_user_id == user_id,
        or_(DeviceShare.expires_at.is_(None), DeviceShare.expires_at > _utcnow()),
    )
    stmt = (
        select(User)
        .where(User.id.in_(select(Device.owner_id).where(Device.id.in_(shared_device_ids))))
        .where(User.is_active.is_(True))
        .order_by(User.username)
    )
    return list(db.execute(stmt).scalars())


_UNSET = object()


def update_device(
    db: Session,
    device: Device,
    *,
    alias: str | None = None,
    name: str | None = None,
    note: str | None = _UNSET,  # type: ignore[assignment]
    owner_id: int | None = _UNSET,  # type: ignore[assignment]
    group_id: int | None = _UNSET,  # type: ignore[assignment]
) -> Device:
    if alias is not None:
        device.alias = alias
    if name is not None:
        device.name = name
    if note is not _UNSET:
        device.note = (note or "").strip() or None
    if owner_id is not _UNSET:
        device.owner_id = owner_id
    if group_id is not _UNSET:
        device.group_id = group_id
    device.updated_at = _utcnow()
    db.flush()
    return device


def delete_device(db: Session, device: Device) -> None:
    db.delete(device)
