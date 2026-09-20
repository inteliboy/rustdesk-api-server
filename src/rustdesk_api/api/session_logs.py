"""Management view of client-reported connection, file-transfer and alarm logs
(/api/v1/connection-logs, /api/v1/file-logs, /api/v1/alarm-logs).

Visibility mirrors devices: administrators see everything, other users only
logs for devices they own or that are actively shared with them. Asking for
a specific device the caller cannot see is a 404, not a 403, so the
endpoint cannot be used to confirm a device exists (CLAUDE.md section 65).

Deleting logs (one, or all / one device's) is administrator-only, even for a
device's owner: these are the audit trail of who connected to a machine, so
the machine's owner must not be able to erase it. Each deletion is itself
recorded in the audit log (counts and ids only, never log contents).
"""

from __future__ import annotations

import datetime
import json

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_current_admin, get_current_user, verify_csrf
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.user import User
from rustdesk_api.security.permissions import can_view_device
from rustdesk_api.services import audit as admin_audit_service
from rustdesk_api.services import client_audit as audit_service
from rustdesk_api.services import devices as device_service

router = APIRouter(prefix="/api/v1", tags=["logs"])


class ConnectionLogOut(BaseModel):
    id: int
    device_id: int | None
    rustdesk_id: str
    device_label: str | None
    from_ip: str | None
    peer_id: str | None
    peer_name: str | None
    conn_type: int | None
    primary_auth: int | None
    two_factor: int | None
    started_at: datetime.datetime
    ended_at: datetime.datetime | None


class ConnectionLogListResponse(BaseModel):
    items: list[ConnectionLogOut]
    page: int
    page_size: int
    total: int


class FileTransferLogOut(BaseModel):
    id: int
    device_id: int | None
    rustdesk_id: str
    device_label: str | None
    peer_id: str | None
    peer_name: str | None
    from_ip: str | None
    audit_type: int | None
    path: str | None
    is_file: bool
    num: int | None
    files: list[list] = []
    logged_at: datetime.datetime


class FileTransferLogListResponse(BaseModel):
    items: list[FileTransferLogOut]
    page: int
    page_size: int
    total: int


class AlarmLogOut(BaseModel):
    id: int
    device_id: int | None
    rustdesk_id: str
    device_label: str | None
    alarm_type: int | None
    from_ip: str | None
    peer_id: str | None
    peer_name: str | None
    conn_type: str | None
    message: str | None
    logged_at: datetime.datetime


class AlarmLogListResponse(BaseModel):
    items: list[AlarmLogOut]
    page: int
    page_size: int
    total: int


def _check_device_filter(db: Session, user: User, device_id: int | None) -> None:
    if device_id is None:
        return
    device = device_service.get_by_id(db, device_id)
    if device is None or not can_view_device(user, device):
        raise ApiError("DEVICE_NOT_FOUND", "The requested device does not exist.", 404)


def _parse_files(raw: str | None) -> list[list]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


@router.get("/connection-logs", response_model=ConnectionLogListResponse)
def list_connection_logs(
    device_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConnectionLogListResponse:
    _check_device_filter(db, user, device_id)
    rows, total = audit_service.list_connection_logs(
        db, user=user, device_id=device_id, page=page, page_size=page_size
    )
    labels = audit_service.device_labels(db, {r.device_id for r in rows if r.device_id is not None})
    items = [
        ConnectionLogOut(
            id=r.id,
            device_id=r.device_id,
            rustdesk_id=r.rustdesk_id,
            device_label=labels.get(r.device_id) if r.device_id is not None else None,
            from_ip=r.from_ip,
            peer_id=r.peer_id,
            peer_name=r.peer_name,
            conn_type=r.conn_type,
            primary_auth=r.primary_auth,
            two_factor=r.two_factor,
            started_at=r.started_at,
            ended_at=r.ended_at,
        )
        for r in rows
    ]
    return ConnectionLogListResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/file-logs", response_model=FileTransferLogListResponse)
def list_file_logs(
    device_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileTransferLogListResponse:
    _check_device_filter(db, user, device_id)
    rows, total = audit_service.list_file_logs(
        db, user=user, device_id=device_id, page=page, page_size=page_size
    )
    labels = audit_service.device_labels(db, {r.device_id for r in rows if r.device_id is not None})
    items = [
        FileTransferLogOut(
            id=r.id,
            device_id=r.device_id,
            rustdesk_id=r.rustdesk_id,
            device_label=labels.get(r.device_id) if r.device_id is not None else None,
            peer_id=r.peer_id,
            peer_name=r.peer_name,
            from_ip=r.from_ip,
            audit_type=r.audit_type,
            path=r.path,
            is_file=r.is_file,
            num=r.num,
            files=_parse_files(r.files),
            logged_at=r.logged_at,
        )
        for r in rows
    ]
    return FileTransferLogListResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/alarm-logs", response_model=AlarmLogListResponse)
def list_alarm_logs(
    device_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AlarmLogListResponse:
    _check_device_filter(db, user, device_id)
    rows, total = audit_service.list_alarm_logs(
        db, user=user, device_id=device_id, page=page, page_size=page_size
    )
    labels = audit_service.device_labels(db, {r.device_id for r in rows if r.device_id is not None})
    items = [
        AlarmLogOut(
            id=r.id,
            device_id=r.device_id,
            rustdesk_id=r.rustdesk_id,
            device_label=labels.get(r.device_id) if r.device_id is not None else None,
            alarm_type=r.alarm_type,
            from_ip=r.from_ip,
            peer_id=r.peer_id,
            peer_name=r.peer_name,
            conn_type=r.conn_type,
            message=r.message,
            logged_at=r.logged_at,
        )
        for r in rows
    ]
    return AlarmLogListResponse(items=items, page=page, page_size=page_size, total=total)


class DeletedCountResponse(BaseModel):
    deleted: int


@router.delete("/connection-logs/{log_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def delete_connection_log(
    log_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)
) -> None:
    row = audit_service.delete_connection_log(db, log_id)
    if row is None:
        raise ApiError("LOG_NOT_FOUND", "The requested log entry does not exist.", 404)
    admin_audit_service.record(
        db,
        action="connection_log_deleted",
        actor_id=admin.id,
        target_type="connection_log",
        target_id=log_id,
        detail={"rustdesk_id": row.rustdesk_id},
    )
    db.commit()


@router.delete("/file-logs/{log_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def delete_file_log(
    log_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)
) -> None:
    row = audit_service.delete_file_log(db, log_id)
    if row is None:
        raise ApiError("LOG_NOT_FOUND", "The requested log entry does not exist.", 404)
    admin_audit_service.record(
        db,
        action="file_log_deleted",
        actor_id=admin.id,
        target_type="file_log",
        target_id=log_id,
        detail={"rustdesk_id": row.rustdesk_id},
    )
    db.commit()


@router.delete("/connection-logs", response_model=DeletedCountResponse, dependencies=[Depends(verify_csrf)])
def clear_connection_logs(
    device_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> DeletedCountResponse:
    """Deletes ALL connection logs, or only `device_id`'s when given."""
    deleted = audit_service.clear_connection_logs(db, device_id=device_id)
    admin_audit_service.record(
        db,
        action="connection_logs_cleared",
        actor_id=admin.id,
        detail={"deleted": deleted, "device_id": device_id},
    )
    db.commit()
    return DeletedCountResponse(deleted=deleted)


@router.delete("/file-logs", response_model=DeletedCountResponse, dependencies=[Depends(verify_csrf)])
def clear_file_logs(
    device_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> DeletedCountResponse:
    """Deletes ALL file-transfer logs, or only `device_id`'s when given."""
    deleted = audit_service.clear_file_logs(db, device_id=device_id)
    admin_audit_service.record(
        db,
        action="file_logs_cleared",
        actor_id=admin.id,
        detail={"deleted": deleted, "device_id": device_id},
    )
    db.commit()
    return DeletedCountResponse(deleted=deleted)


@router.delete("/alarm-logs/{log_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def delete_alarm_log(
    log_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)
) -> None:
    row = audit_service.delete_alarm_log(db, log_id)
    if row is None:
        raise ApiError("LOG_NOT_FOUND", "The requested log entry does not exist.", 404)
    admin_audit_service.record(
        db,
        action="alarm_log_deleted",
        actor_id=admin.id,
        target_type="alarm_log",
        target_id=log_id,
        detail={"rustdesk_id": row.rustdesk_id},
    )
    db.commit()


@router.delete("/alarm-logs", response_model=DeletedCountResponse, dependencies=[Depends(verify_csrf)])
def clear_alarm_logs(
    device_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> DeletedCountResponse:
    """Deletes ALL alarm logs, or only `device_id`'s when given."""
    deleted = audit_service.clear_alarm_logs(db, device_id=device_id)
    admin_audit_service.record(
        db,
        action="alarm_logs_cleared",
        actor_id=admin.id,
        detail={"deleted": deleted, "device_id": device_id},
    )
    db.commit()
    return DeletedCountResponse(deleted=deleted)
