"""Device operations that go beyond one record's fields (/api/v1/devices/...):
bulk changes, the activity timeline, and deciding on a parked uuid change.

Every device is checked one by one with the same permission rules as the
single-device endpoints (api.devices); a device the caller cannot see is
reported as not found, so none of this confirms that an id exists.
"""

from __future__ import annotations

import datetime
import json
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_current_user, verify_csrf
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.audit import AuditLog
from rustdesk_api.models.client_audit import ConnectionLog
from rustdesk_api.models.device import Device
from rustdesk_api.models.device_event import DeviceEvent
from rustdesk_api.models.user import User
from rustdesk_api.security.permissions import (
    can_delete_device,
    can_edit_device,
    can_manage_connections,
    can_view_device,
)
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import client_audit
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import groups as group_service
from rustdesk_api.services import strategies as strategy_service
from rustdesk_api.services import tags as tag_service

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])

MAX_BULK = 200


def _get_visible_or_404(db: Session, device_id: int, user: User) -> Device:
    device = device_service.get_by_id(db, device_id)
    if device is None or not can_view_device(user, device):
        raise ApiError("DEVICE_NOT_FOUND", "The requested device does not exist.", 404)
    return device


# ---------------------------------------------------------------------------
# Bulk changes
# ---------------------------------------------------------------------------


class BulkRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=MAX_BULK)
    action: Literal["add_tag", "remove_tag", "set_group", "set_owner", "set_strategy", "delete"]
    tag_id: int | None = None
    # For the three "set_" actions an explicit null clears the value.
    group_id: int | None = None
    owner_id: int | None = None
    strategy_id: int | None = None

    @model_validator(mode="after")
    def _check(self) -> BulkRequest:
        if self.action in ("add_tag", "remove_tag") and self.tag_id is None:
            raise ValueError("tag_id is required for this action")
        needs = {"set_group": "group_id", "set_owner": "owner_id", "set_strategy": "strategy_id"}
        field = needs.get(self.action)
        if field and field not in self.model_fields_set:
            raise ValueError(f"{field} is required for this action (null clears it)")
        return self


class Skipped(BaseModel):
    id: int
    reason: str  # "not_found" | "forbidden"


class BulkResult(BaseModel):
    updated: list[int]
    skipped: list[Skipped]


@router.post("/bulk", response_model=BulkResult, dependencies=[Depends(verify_csrf)])
def bulk_update(
    payload: BulkRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> BulkResult:
    action = payload.action
    # The target of the action is validated once, up front: a bad tag, group,
    # owner or strategy fails the whole request instead of every device.
    tag = group = strategy = None
    owner_id: int | None = None
    if action in ("add_tag", "remove_tag"):
        tag = tag_service.get_by_id(db, payload.tag_id) if payload.tag_id is not None else None
        if tag is None:
            raise ApiError("TAG_NOT_FOUND", "The requested tag does not exist.", 404)
    elif action == "set_group":
        if payload.group_id is not None:
            group = group_service.get_by_id(db, payload.group_id)
            if group is None or (not user.is_admin and group.owner_id != user.id):
                raise ApiError("GROUP_NOT_FOUND", "The requested group does not exist.", 404)
    elif action == "set_owner":
        if not user.is_admin:
            raise ApiError("FORBIDDEN", "Only administrators may reassign device ownership.", 403)
        if payload.owner_id is not None:
            if db.get(User, payload.owner_id) is None:
                raise ApiError("USER_NOT_FOUND", "The requested user does not exist.", 404)
            owner_id = payload.owner_id
    elif action == "set_strategy":
        if not user.is_admin:
            raise ApiError("FORBIDDEN", "Only administrators may assign strategies.", 403)
        if payload.strategy_id is not None:
            strategy = strategy_service.get_by_id(db, payload.strategy_id)
            if strategy is None:
                raise ApiError("STRATEGY_NOT_FOUND", "The requested strategy does not exist.", 404)

    updated: list[int] = []
    skipped: list[Skipped] = []
    for device_id in dict.fromkeys(payload.ids):  # ignore repeats, keep order
        device = device_service.get_by_id(db, device_id)
        if device is None or not can_view_device(user, device):
            skipped.append(Skipped(id=device_id, reason="not_found"))
            continue
        allowed = can_delete_device(user, device) if action == "delete" else can_edit_device(user, device)
        if action in ("set_owner", "set_strategy"):
            allowed = user.is_admin
        if not allowed:
            skipped.append(Skipped(id=device_id, reason="forbidden"))
            continue

        detail: dict = {"rustdesk_id": device.rustdesk_id, "via": "bulk"}
        audit_action = "device_updated"
        if action == "add_tag" and tag is not None:
            if tag not in device.tags:
                tag_service.set_device_tags(db, device, [*device.tags, tag])
            detail["tag_added"] = tag.name
        elif action == "remove_tag" and tag is not None:
            tag_service.set_device_tags(db, device, [t for t in device.tags if t.id != tag.id])
            detail["tag_removed"] = tag.name
        elif action == "set_group":
            device_service.update_device(db, device, group_id=group.id if group else None)
            detail["group"] = group.name if group else None
        elif action == "set_owner":
            device_service.update_device(db, device, owner_id=owner_id)
            detail["owner_id"] = owner_id
        elif action == "set_strategy":
            device.strategy_id = strategy.id if strategy else None
            audit_action = "strategy_assigned"
            detail["strategy"] = strategy.name if strategy else None
        elif action == "delete":
            audit_action = "device_deleted"
            device_service.delete_device(db, device)

        audit_service.record(
            db,
            action=audit_action,
            actor_id=user.id,
            target_type="device",
            target_id=device_id,
            detail=detail,
        )
        updated.append(device_id)
    db.commit()
    return BulkResult(updated=updated, skipped=skipped)


# ---------------------------------------------------------------------------
# A parked uuid change
# ---------------------------------------------------------------------------


class UuidDecision(BaseModel):
    pending: bool = False


def _decide(db: Session, device_id: int, user: User, accept: bool) -> UuidDecision:
    device = _get_visible_or_404(db, device_id, user)
    # Owner or administrator only - a share, even with "control", must not be
    # able to decide which machine a device is.
    if not can_delete_device(user, device):
        raise ApiError("DEVICE_NOT_FOUND", "The requested device does not exist.", 404)
    changed = (
        device_service.accept_pending_uuid(db, device)
        if accept
        else device_service.reject_pending_uuid(db, device)
    )
    if not changed:
        raise ApiError("NO_PENDING_CHANGE", "There is no pending change for this device.", 409)
    audit_service.record(
        db,
        action="device_uuid_accepted" if accept else "device_uuid_rejected",
        actor_id=user.id,
        target_type="device",
        target_id=device.id,
        detail={"rustdesk_id": device.rustdesk_id},
    )
    db.commit()
    return UuidDecision(pending=device.pending_uuid is not None)


@router.post("/{device_id}/uuid/accept", response_model=UuidDecision, dependencies=[Depends(verify_csrf)])
def accept_uuid(
    device_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> UuidDecision:
    """The machine that uploaded a different uuid is this device (for example
    after a reinstall). From now on its heartbeats are the ones that count."""
    return _decide(db, device_id, user, accept=True)


@router.post("/{device_id}/uuid/reject", response_model=UuidDecision, dependencies=[Depends(verify_csrf)])
def reject_uuid(
    device_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> UuidDecision:
    return _decide(db, device_id, user, accept=False)


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


class TimelineEntry(BaseModel):
    at: datetime.datetime
    source: Literal["event", "audit", "connection"]
    kind: str
    actor: str | None = None
    result: str | None = None
    detail: dict = {}


def _detail(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _aware(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


@router.get("/{device_id}/timeline", response_model=list[TimelineEntry])
def timeline(
    device_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    before: datetime.datetime | None = Query(
        default=None, description="Only entries older than this (to page backwards)."
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TimelineEntry]:
    """What happened to this device, newest first: it came online, connections
    made to it, changes to it (edits, shares, strategy, disconnects) and
    attempts by another install to take its id."""
    device = _get_visible_or_404(db, device_id, user)
    cutoff = _aware(before) if before is not None else None
    entries: list[TimelineEntry] = []

    events = select(DeviceEvent).where(DeviceEvent.device_id == device.id)
    if cutoff is not None:
        events = events.where(DeviceEvent.created_at < cutoff)
    for e in db.execute(events.order_by(DeviceEvent.created_at.desc()).limit(limit)).scalars():
        entries.append(TimelineEntry(at=e.created_at, source="event", kind=e.kind, detail=_detail(e.detail)))

    # Who changed or shared the device (and with whom) is for its owner and
    # administrators; someone it is merely shared with sees events and connections.
    audit_rows = []
    if can_manage_connections(user, device):
        audits = (
            select(AuditLog, User.username)
            .join(User, User.id == AuditLog.actor_id, isouter=True)
            .where(AuditLog.target_type == "device", AuditLog.target_id == device.id)
        )
        if cutoff is not None:
            audits = audits.where(AuditLog.created_at < cutoff)
        audit_rows = db.execute(audits.order_by(AuditLog.created_at.desc()).limit(limit)).all()
    for row, actor in audit_rows:
        entries.append(
            TimelineEntry(
                at=row.created_at,
                source="audit",
                kind=row.action,
                actor=actor,
                result=row.result,
                detail=_detail(row.detail),
            )
        )

    connections = select(ConnectionLog).where(ConnectionLog.device_id == device.id)
    if cutoff is not None:
        connections = connections.where(ConnectionLog.started_at < cutoff)
    logs = list(db.execute(connections.order_by(ConnectionLog.started_at.desc()).limit(limit)).scalars())
    names = client_audit.restore_peer_names(db, {(c.peer_id, c.peer_name) for c in logs})
    for c in logs:
        entries.append(
            TimelineEntry(
                at=c.started_at,
                source="connection",
                kind="connection",
                detail={
                    "peer_id": c.peer_id,
                    "peer_name": names.get((c.peer_id or "", c.peer_name or ""), c.peer_name),
                    "from_ip": c.from_ip,
                    "conn_type": c.conn_type,
                    "ended_at": c.ended_at.isoformat() if c.ended_at else None,
                },
            )
        )

    entries.sort(key=lambda e: _aware(e.at), reverse=True)
    return entries[:limit]
