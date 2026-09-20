"""RustDesk client heartbeat and system-info reporting.

See CLAUDE.md section 17 (heartbeat handling) and section 73 (compatibility
reference). Not yet verified against a real RustDesk desktop client - see
docs/rustdesk-compatibility.md.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_client_ip, get_settings_dep
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.models.device import Device
from rustdesk_api.security.encryption import get_secret_box
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import connections as connection_service
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import enrollment as enrollment_service
from rustdesk_api.services import heartbeat as heartbeat_service
from rustdesk_api.services import strategies as strategy_service
from rustdesk_api.services import tokens as token_service
from rustdesk_api.ws import device_snapshot_for_broadcast

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rustdesk-compat"])


def _schedule_broadcast(
    request: Request, background_tasks: BackgroundTasks, device: Device | None, settings: Settings
) -> None:
    """Live-updates the WebUI's Devices page (CLAUDE.md section 47) without
    blocking this response on it - scheduled as a BackgroundTask so it runs
    after the response is sent, using a plain-data snapshot taken now
    (while the request's DB session is still open) rather than the ORM
    object itself (see rustdesk_api.ws.device_snapshot_for_broadcast)."""
    if device is None:
        return
    payload, owner_id, shared_with = device_snapshot_for_broadcast(device, settings.device_online_timeout)
    manager = request.app.state.ws_device_manager
    background_tasks.add_task(
        manager.notify_device_change,
        device_owner_id=owner_id,
        shared_with_user_ids=shared_with,
        payload=payload,
    )


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


class RustdeskIdUuidRequest(BaseModel):
    id: str | None = None
    uuid: str | None = None


def _same_install(device: Device, uuid: str | None) -> bool:
    if device.uuid is None:
        return True
    return uuid is not None and hmac.compare_digest(device.uuid.encode(), uuid.encode())


class RustdeskHeartbeatRequest(RustdeskIdUuidRequest):
    # Both are read defensively: a value of an unexpected type is ignored
    # rather than turning the heartbeat into a validation error.
    modified_at: Any = None
    conns: Any = None


@router.post("/api/heartbeat")
def rustdesk_heartbeat(
    payload: RustdeskHeartbeatRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    client_ip: str | None = Depends(get_client_ip),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    if not payload.id:
        return {"error": "Missing device id."}
    request.app.state.counters.inc("heartbeats")
    device = device_service.get_by_rustdesk_id(db, payload.id)
    if device is None:
        # We do not know this device (new database, or an administrator deleted
        # it). The client only re-sends its system info when the previous send
        # was not acknowledged, and `/api/sysinfo` now acknowledges it, so ask
        # for it explicitly: the client's own `"sysinfo"` heartbeat-response key.
        return {"data": "OK", "sysinfo": True}

    response: dict = {"data": "OK"}
    if not _same_install(device, payload.uuid):
        # The heartbeat is unauthenticated, so the client's own `uuid` (which
        # only that install and its sysinfo upload carry) is the only thing
        # tying it to this device. Without it, nobody gets policy, pending
        # disconnects, or a say in which connections are recorded - and the
        # heartbeat does not even make the device look online.
        return response
    heartbeat_service.handle_heartbeat(
        db,
        rustdesk_id=payload.id,
        uuid=payload.uuid,
        ip_address=client_ip,
        online_timeout=settings.device_online_timeout,
    )
    to_close = connection_service.record_heartbeat(db, device, payload.conns)
    if to_close:
        response["disconnect"] = to_close
    modified_at = payload.modified_at if isinstance(payload.modified_at, int) else None
    response.update(strategy_service.heartbeat_fragment(db, device, modified_at))
    db.commit()
    _schedule_broadcast(request, background_tasks, device, settings)
    return response


class RustdeskSysinfoRequest(BaseModel):
    # Clients with `preset-*` options add those keys; they are read from
    # `model_extra`, and only when ALLOW_SYSINFO_PRESETS is on.
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    uuid: str | None = None
    cpu: str | None = None
    hostname: str | None = None
    memory: str | None = None
    os: str | None = None
    username: str | None = None
    version: str | None = None


def _apply_presets(db: Session, device: Device, extra: dict, settings: Settings) -> None:
    """A newly registered device's `preset-*` options (see services.enrollment).
    A preset that cannot be honoured must never stand in the way of the device
    registering, so problems are logged and skipped."""
    assignment = enrollment_service.assignment_from_body(extra, enrollment_service.PRESET_KEYS)
    if assignment.is_empty():
        return
    try:
        with db.begin_nested():
            enrollment_service.apply(
                db,
                device,
                assignment,
                actor=None,
                box=get_secret_box(settings.data_encryption_key),
                strict=False,
            )
    except enrollment_service.EnrollmentError as exc:
        logger.info("Presets for new device %s were not applied: %s", device.id, exc)
        return
    audit_service.record(
        db,
        action="device_preset_applied",
        target_type="device",
        target_id=device.id,
        detail={"rustdesk_id": device.rustdesk_id, "via": "sysinfo", **assignment.redacted()},
    )


@router.post("/api/sysinfo")
def rustdesk_sysinfo(
    payload: RustdeskSysinfoRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    client_ip: str | None = Depends(get_client_ip),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    if not payload.id:
        return JSONResponse({"error": "Missing device id."})
    request.app.state.counters.inc("sysinfo_uploads")

    raw_token = _extract_bearer(authorization)
    session_obj = token_service.get_valid_session(db, raw_token) if raw_token else None
    owner_id = session_obj.user.id if session_obj is not None else None

    # Confirmed against a real 1.4.9 Windows client (see
    # docs/rustdesk-compatibility.md): `os` is a composite
    # "<platform> / <full OS description>" string, e.g.
    # "windows / Windows 11 IoT Enterprise LTSC 2024 - 11 (26300)", not a
    # bare platform name. Split it so `platform` stays a short, filterable
    # value and the fuller description lands in `os_version`. Falls back to
    # storing the whole string in `platform` if no " / " separator is
    # present (unconfirmed on non-Windows clients).
    platform, os_version = None, None
    if payload.os:
        parts = payload.os.split(" / ", 1)
        platform = parts[0] or None
        os_version = parts[1] if len(parts) > 1 else None

    existing = device_service.get_by_rustdesk_id(db, payload.id)
    is_new = existing is None
    # An upload that carries the login token of the device's owner (or of an
    # administrator) proves who is behind it, so it may re-bind the device to a
    # new uuid; an anonymous one may not (see DEVICE_UUID_REBIND).
    trusted = (
        session_obj is not None
        and session_obj.user.is_active
        and (existing is None or session_obj.user.is_admin or existing.owner_id == session_obj.user.id)
    )
    device = device_service.register_or_update(
        db,
        rustdesk_id=payload.id,
        uuid=payload.uuid,
        hostname=payload.hostname,
        username=payload.username,
        platform=platform,
        os_version=os_version,
        client_version=payload.version,
        ip_address=client_ip,
        cpu=payload.cpu,
        memory=payload.memory,
        owner_id=owner_id,
        uuid_policy=settings.device_uuid_rebind,
        trusted=trusted,
        online_timeout=settings.device_online_timeout,
    )
    if is_new and settings.allow_sysinfo_presets:
        _apply_presets(db, device, payload.model_extra or {}, settings)
    db.commit()
    _schedule_broadcast(request, background_tasks, device, settings)
    # The client compares the body with this exact text (`hbbs_http/sync.rs`).
    # Anything else counts as "not uploaded" and it sends the same data again
    # every ~2 minutes. See docs/rustdesk-compatibility.md for the side effect.
    return PlainTextResponse("SYSINFO_UPDATED")
