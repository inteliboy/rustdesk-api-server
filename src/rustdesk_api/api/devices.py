"""Management REST API for devices (/api/v1/devices/...).

Every handler enforces ownership/admin/share authorization server-side -
never rely on the WebUI hiding buttons (CLAUDE.md sections 12, 66). IDOR is
explicitly in scope: GET /api/v1/devices/{id} must not leak another user's
device merely because the id is guessable (CLAUDE.md section 65).
"""

from __future__ import annotations

import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_current_user, get_settings_dep, verify_csrf
from rustdesk_api.api.schemas import DeviceListResponse, DeviceOut, TagOut, UpdateDeviceRequest
from rustdesk_api.clientversion import is_older
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.device import Device
from rustdesk_api.models.user import User
from rustdesk_api.security.permissions import (
    can_delete_device,
    can_edit_device,
    can_manage_connections,
    can_view_device,
)
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import connections as connection_service
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import groups as group_service
from rustdesk_api.services import strategies as strategy_service
from rustdesk_api.services import tags as tag_service

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])


def _effective_strategy_name(device: Device, default_name: str | None) -> str | None:
    if device.strategy_id is not None:
        return device.strategy.name if device.strategy else None
    if device.group is not None and device.group.strategy_id is not None:
        return device.group.strategy.name if device.group.strategy else None
    return default_name


def _to_out(device: Device, settings: Settings, default_strategy_name: str | None = None) -> DeviceOut:
    timeout = settings.device_online_timeout
    return DeviceOut(
        id=device.id,
        rustdesk_id=device.rustdesk_id,
        name=device.name,
        hostname=device.hostname,
        alias=device.alias,
        username=device.username,
        platform=device.platform,
        os_version=device.os_version,
        client_version=device.client_version,
        ip_address=device.ip_address,
        api_scheme=device.api_scheme,
        cpu=device.cpu,
        memory=device.memory,
        note=device.note,
        last_seen=device.last_seen,
        owner_id=device.owner_id,
        owner_username=device.owner.username if device.owner else None,
        group_id=device.group_id,
        group_name=device.group.name if device.group else None,
        strategy_id=device.strategy_id,
        strategy_name=device.strategy.name if device.strategy is not None else None,
        effective_strategy_name=_effective_strategy_name(device, default_strategy_name),
        connection_count=len(device.connection_ids),
        uuid_change_pending=device.pending_uuid is not None,
        uuid_change_at=device.pending_uuid_at,
        uuid_change_ip=device.pending_uuid_ip,
        tags=[TagOut.model_validate(t) for t in device.tags],
        created_at=device.created_at,
        updated_at=device.updated_at,
        online=device.is_online(timeout),
        watch_offline=device.watch_offline,
        archived=device.archived_at is not None,
        approval=device.approval,
        outdated=is_older(device.client_version, settings.min_client_version),
    )


def _default_strategy_name(db: Session) -> str | None:
    default = strategy_service.get_default(db)
    return default.name if default is not None else None


def _get_visible_or_404(db: Session, device_id: int, user: User) -> Device:
    device = device_service.get_by_id(db, device_id)
    if device is None or not can_view_device(user, device):
        # Same error for "not found" and "not yours" - do not confirm the
        # existence of devices the caller cannot see.
        raise ApiError("DEVICE_NOT_FOUND", "The requested device does not exist.", 404)
    return device


@router.get("", response_model=DeviceListResponse)
def list_devices(
    search: str | None = Query(default=None),
    group_id: int | None = Query(default=None),
    tag_id: int | None = Query(default=None),
    status: Literal["online", "offline", "archived", "pending", "rejected"] | None = Query(default=None),
    sort: Literal["last_seen", "created", "name", "id"] = Query(default="last_seen"),
    order: Literal["asc", "desc"] | None = Query(default=None),
    owner_id: int | None = Query(
        default=None, description="Admin-only filter to a specific owner; ignored for non-admins."
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings_dep),
) -> DeviceListResponse:
    if status in ("pending", "rejected") and not user.is_admin:
        raise ApiError("FORBIDDEN", "Only administrators may list devices awaiting approval.", 403)
    # A non-admin can only ever see their own (+ shared) devices, regardless
    # of what `owner_id` they pass - the server enforces this, never the
    # client (CLAUDE.md section 66).
    effective_owner_id = owner_id if user.is_admin else user.id
    shared_with_user_id = None if user.is_admin else user.id
    items, total = device_service.list_devices(
        db,
        owner_id=effective_owner_id,
        shared_with_user_id=shared_with_user_id,
        search=search,
        group_id=group_id,
        tag_id=tag_id,
        status=status,
        online_timeout=settings.device_online_timeout,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
    )
    default_name = _default_strategy_name(db)
    return DeviceListResponse(
        items=[_to_out(d, settings, default_name) for d in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/{device_id}", response_model=DeviceOut)
def get_device(
    device_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings_dep),
) -> DeviceOut:
    device = _get_visible_or_404(db, device_id, user)
    return _to_out(device, settings, _default_strategy_name(db))


@router.patch("/{device_id}", response_model=DeviceOut, dependencies=[Depends(verify_csrf)])
def update_device(
    device_id: int,
    payload: UpdateDeviceRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings_dep),
) -> DeviceOut:
    device = _get_visible_or_404(db, device_id, user)
    if not can_edit_device(user, device):
        raise ApiError("DEVICE_NOT_FOUND", "The requested device does not exist.", 404)

    if payload.owner_id is not None and not can_delete_device(user, device):
        raise ApiError("FORBIDDEN", "Only administrators may reassign device ownership.", 403)

    if payload.group_id is not None:
        group = group_service.get_by_id(db, payload.group_id)
        if group is None or (not user.is_admin and group.owner_id != user.id):
            raise ApiError("GROUP_NOT_FOUND", "The requested group does not exist.", 404)

    update_kwargs: dict = {"alias": payload.alias, "name": payload.name}
    if "note" in payload.model_fields_set:
        update_kwargs["note"] = payload.note
    if payload.owner_id is not None:
        update_kwargs["owner_id"] = payload.owner_id
    if payload.group_id is not None:
        update_kwargs["group_id"] = payload.group_id
    device_service.update_device(db, device, **update_kwargs)

    if payload.tag_ids is not None:
        tags = [t for t in (tag_service.get_by_id(db, tid) for tid in payload.tag_ids) if t is not None]
        tag_service.set_device_tags(db, device, tags)

    audit_service.record(
        db,
        action="device_updated",
        actor_id=user.id,
        target_type="device",
        target_id=device.id,
        detail={"rustdesk_id": device.rustdesk_id, "alias": device.alias},
    )
    db.commit()
    return _to_out(device, settings, _default_strategy_name(db))


@router.delete("/{device_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def delete_device(
    device_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    device = _get_visible_or_404(db, device_id, user)
    if not can_delete_device(user, device):
        raise ApiError("DEVICE_NOT_FOUND", "The requested device does not exist.", 404)
    deleted_rustdesk_id = device.rustdesk_id
    deleted_alias = device.alias
    device_service.delete_device(db, device)
    audit_service.record(
        db,
        action="device_deleted",
        actor_id=user.id,
        target_type="device",
        target_id=device_id,
        detail={"rustdesk_id": deleted_rustdesk_id, "alias": deleted_alias},
    )
    db.commit()


class ConnectionOut(BaseModel):
    id: int
    # What the client's own connection log says about who is connecting; empty
    # when it has not (yet) reported that connection.
    peer_id: str | None = None
    peer_name: str | None = None
    from_ip: str | None = None
    started_at: datetime.datetime | None = None
    disconnect_requested: bool = False


class DisconnectRequest(BaseModel):
    # None ends every connection the device currently reports.
    connection_ids: list[int] | None = None


@router.get("/{device_id}/connections", response_model=list[ConnectionOut])
def list_connections(
    device_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[ConnectionOut]:
    """The incoming connections the device reported at its last heartbeat."""
    device = _get_visible_or_404(db, device_id, user)
    return [ConnectionOut(**row) for row in connection_service.describe(db, device)]


@router.post(
    "/{device_id}/disconnect", response_model=list[ConnectionOut], dependencies=[Depends(verify_csrf)]
)
def disconnect_connections(
    device_id: int,
    payload: DisconnectRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ConnectionOut]:
    """Ask the device to end connections. It happens at the client's next
    heartbeat (a few seconds while a connection is open), not instantly."""
    device = _get_visible_or_404(db, device_id, user)
    if not can_manage_connections(user, device):
        raise ApiError("DEVICE_NOT_FOUND", "The requested device does not exist.", 404)
    try:
        queued = connection_service.request_disconnect(device, payload.connection_ids)
    except ValueError as exc:
        raise ApiError("INVALID_CONNECTION", str(exc), 422) from exc
    if queued:
        audit_service.record(
            db,
            action="connection_disconnect_requested",
            actor_id=user.id,
            target_type="device",
            target_id=device.id,
            detail={"rustdesk_id": device.rustdesk_id, "connection_ids": queued},
        )
    db.commit()
    return [ConnectionOut(**row) for row in connection_service.describe(db, device)]
