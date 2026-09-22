"""Admin dashboard stats and audit log access."""

from __future__ import annotations

import datetime
import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_current_admin, get_settings_dep, require_permission
from rustdesk_api.api.schemas import DashboardStats
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.models.audit import AuditLog
from rustdesk_api.models.device import Device
from rustdesk_api.models.group import Group
from rustdesk_api.models.tag import Tag
from rustdesk_api.models.user import User
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import fleet as fleet_service
from rustdesk_api.services import server_metrics
from rustdesk_api.services import strategies as strategy_service

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/dashboard", response_model=DashboardStats)
def dashboard_stats(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
    settings: Settings = Depends(get_settings_dep),
) -> DashboardStats:
    live = Device.archived_at.is_(None) & (Device.approval == device_service.APPROVED)
    total_devices = db.execute(select(func.count(Device.id)).where(live)).scalar_one()
    total_users = db.execute(select(func.count(User.id))).scalar_one()
    total_groups = db.execute(select(func.count(Group.id))).scalar_one()
    total_tags = db.execute(select(func.count(Tag.id))).scalar_one()

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=settings.device_online_timeout
    )
    online_devices = db.execute(
        select(func.count(Device.id)).where(live, Device.last_seen.is_not(None), Device.last_seen >= cutoff)
    ).scalar_one()
    new_devices = db.execute(
        select(func.count(Device.id)).where(
            live,
            Device.created_at >= datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24),
        )
    ).scalar_one()
    archived_devices = db.execute(
        select(func.count(Device.id)).where(
            Device.archived_at.is_not(None), Device.approval == device_service.APPROVED
        )
    ).scalar_one()
    # Nothing of its own, nothing through its group (or no group), and no default.
    has_default = strategy_service.get_default(db) is not None
    without_strategy = 0
    if not has_default:
        group_has_strategy = select(Group.id).where(Group.strategy_id.is_not(None))
        without_strategy = db.execute(
            select(func.count(Device.id)).where(
                live,
                Device.strategy_id.is_(None),
                (Device.group_id.is_(None)) | (Device.group_id.not_in(group_has_strategy)),
            )
        ).scalar_one()

    return DashboardStats(
        total_devices=total_devices,
        online_devices=online_devices,
        offline_devices=total_devices - online_devices,
        total_users=total_users,
        total_groups=total_groups,
        total_tags=total_tags,
        new_devices_24h=new_devices,
        outdated_devices=fleet_service.count_outdated(db, settings),
        archived_devices=archived_devices,
        devices_without_strategy=without_strategy,
        pending_devices=device_service.count_pending(db),
    )


class MetricSample(BaseModel):
    t: float
    process_cpu: float
    system_cpu: float
    process_memory: int
    system_memory_percent: float
    threads: int


class ServerStatus(BaseModel):
    """Hardware/software facts plus the CPU/memory history. `hardware`,
    `system`, `software`, `database` and `process` are descriptive and vary by
    platform, so they stay loosely typed."""

    hardware: dict[str, Any]
    system: dict[str, Any]
    software: dict[str, Any]
    database: dict[str, Any]
    process: dict[str, Any]
    interval_seconds: int
    history_minutes: int
    samples: list[MetricSample]


@router.get("/server", response_model=ServerStatus)
def server_status(
    request: Request,
    since: float | None = Query(default=None, description="Only samples newer than this Unix time."),
    _admin: User = Depends(get_current_admin),
    settings: Settings = Depends(get_settings_dep),
) -> ServerStatus:
    """What the server runs on and how much of it this process uses.

    The samples come from a rolling in-memory window (per process), so `since`
    lets the WebUI poll for just the new ones."""
    sampler: server_metrics.MetricsSampler | None = getattr(request.app.state, "metrics_sampler", None)
    samples = sampler.samples(since) if sampler else []
    return ServerStatus(
        **server_metrics.describe_host(settings),
        interval_seconds=settings.server_metrics_interval_seconds,
        history_minutes=settings.server_metrics_history_minutes,
        samples=[MetricSample(**server_metrics.sample_to_dict(s)) for s in samples],
    )


class AuditLogOut(BaseModel):
    id: int
    actor_id: int | None
    actor_username: str | None = None
    action: str
    target_type: str | None
    target_id: int | None
    result: str
    ip_address: str | None
    detail: dict[str, Any] | None = None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class AuditLogListResponse(BaseModel):
    items: list[AuditLogOut]
    page: int
    page_size: int
    total: int


@router.get("/audit-logs", response_model=AuditLogListResponse)
def list_audit_logs(
    actor_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    # The console audit trail sits behind "manage" of logs, not "view" (CortenDesk
    # treats sign-in history/the audit trail the same way).
    _user: User = Depends(require_permission("logs", "manage")),
) -> AuditLogListResponse:
    count_stmt = select(func.count(AuditLog.id))
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if actor_id is not None:
        count_stmt = count_stmt.where(AuditLog.actor_id == actor_id)
        stmt = stmt.where(AuditLog.actor_id == actor_id)

    total = db.execute(count_stmt).scalar_one()
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    items = list(db.execute(stmt).scalars())

    actor_ids = {i.actor_id for i in items if i.actor_id is not None}
    usernames: dict[int, str] = {}
    if actor_ids:
        rows = db.execute(select(User.id, User.username).where(User.id.in_(actor_ids))).all()
        usernames = {row.id: row.username for row in rows}

    def _to_out(i: AuditLog) -> AuditLogOut:
        return AuditLogOut(
            id=i.id,
            actor_id=i.actor_id,
            actor_username=usernames.get(i.actor_id) if i.actor_id is not None else None,
            action=i.action,
            target_type=i.target_type,
            target_id=i.target_id,
            result=i.result,
            ip_address=i.ip_address,
            detail=json.loads(i.detail) if i.detail else None,
            created_at=i.created_at,
        )

    return AuditLogListResponse(
        items=[_to_out(i) for i in items], page=page, page_size=page_size, total=total
    )
