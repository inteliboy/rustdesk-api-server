"""The browser web client's launcher: whether it is available, and a session ticket.

The video, input and files never pass through these endpoints - they travel over
the WebSocket bridge (api/webclient_ws.py). This one decides who may start a
session on which device and hands the browser what it needs to start it.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from rustdesk_api import __version__
from rustdesk_api.api.deps import (
    get_client_ip,
    get_current_user,
    get_interactive_user,
    get_settings_dep,
    verify_csrf,
)
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.user import User
from rustdesk_api.security.permissions import can_view_device
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import webclient as webclient_service

router = APIRouter(prefix="/api/v1/webclient", tags=["web client"])

WS_PATH = "/api/v1/webclient/ws"
TICKET_COOKIE = "rd_webclient"


class StatusOut(BaseModel):
    enabled: bool
    available: bool
    # What is missing, for administrators only (it names environment variables).
    problems: list[str] = []


class SessionIn(BaseModel):
    device_id: int = Field(ge=1)


class CheckTargetOut(BaseModel):
    ok: bool
    where: str | None
    error: str | None


class CheckOut(BaseModel):
    ok: bool
    # One sentence for whoever pressed the button; empty when all is well.
    message: str
    # The rest is for administrators.
    hbbs: CheckTargetOut | None = None
    hbbr: CheckTargetOut | None = None
    answer: str | None = None
    last_problem: str | None = None


class SessionOut(BaseModel):
    peer_id: str
    peer_name: str
    # Paths on this server; the page adds ws:// or wss:// and its own host.
    ws_id_path: str
    ws_relay_path: str
    # The ID server's public key (not a secret: every RustDesk client has it).
    key: str
    my_id: str
    my_name: str
    version: str


@router.get("/status", response_model=StatusOut)
def status(
    user: User = Depends(get_current_user), settings: Settings = Depends(get_settings_dep)
) -> StatusOut:
    problems = webclient_service.problems(settings) if settings.web_client_enabled else []
    return StatusOut(
        enabled=settings.web_client_enabled,
        available=webclient_service.available(settings),
        problems=problems if user.is_admin else [],
    )


def _device_for_web_session(db: Session, user: User, settings: Settings, device_id: int):
    """The device this user may open in the browser, or the error to answer with."""
    if not settings.web_client_enabled:
        raise ApiError("WEB_CLIENT_DISABLED", "The web client is not enabled on this server.", 404)
    problems = webclient_service.problems(settings)
    if problems:
        detail = " ".join(problems) if user.is_admin else "It has not been set up yet."
        raise ApiError("WEB_CLIENT_NOT_CONFIGURED", f"The web client is not set up. {detail}", 409)

    device = device_service.get_by_id(db, device_id)
    if device is None or not can_view_device(user, device):
        # Same answer for "no such device" and "not yours", like the rest of the API.
        raise ApiError("DEVICE_NOT_FOUND", "The requested device does not exist.", 404)
    if not device.is_approved:
        raise ApiError(
            "DEVICE_NOT_APPROVED",
            "This device has not been approved yet. Approve it on the Devices page first.",
            409,
        )
    if not webclient_service.can_connect(user, device):
        raise ApiError(
            "SHARE_NOT_ALLOWED",
            "This device is shared with you to view only. A browser session needs the control permission.",
            403,
        )
    return device


@router.get("/check", response_model=CheckOut)
def check_connection(
    request: Request,
    device_id: int = Query(ge=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> CheckOut:
    """Can this server get a session for this device through to hbbs and hbbr? The browser only learns
    that a socket closed; this opens the same sockets from here and asks hbbs the question the browser
    asks, so the answer names the cause (see services/webclient.py). The launcher page calls it before
    it starts the client."""
    device = _device_for_web_session(db, user, settings, device_id)
    # A plain (threadpool) handler has no event loop of its own, so this one is run here.
    result = asyncio.run(webclient_service.check(settings, device.rustdesk_id, client_ip))
    message = webclient_service.summarise(result, admin=user.is_admin)
    if not user.is_admin:
        return CheckOut(ok=not message, message=message)
    last = getattr(request.app.state, "web_client_last", None)
    return CheckOut(
        ok=not message,
        message=message,
        hbbs=CheckTargetOut(**result["hbbs"]),
        hbbr=CheckTargetOut(**result["hbbr"]),
        answer=result["answer"],
        last_problem=f"{last['text']} ({int(time.time() - last['at'])} s ago)" if last else None,
    )


@router.post("/sessions", response_model=SessionOut, dependencies=[Depends(verify_csrf)])
def start_session(
    payload: SessionIn,
    request: Request,
    response: Response,
    user: User = Depends(get_interactive_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> SessionOut:
    device = _device_for_web_session(db, user, settings, payload.device_id)

    audit_service.record(
        db,
        action="webclient_session",
        actor_id=user.id,
        target_type="device",
        target_id=device.id,
        ip_address=client_ip,
        detail={"rustdesk_id": device.rustdesk_id},
    )
    db.commit()

    # The ticket travels as a cookie, not in the WebSocket URL: the address of a
    # WebSocket is written to access logs, and a token must not be.
    response.set_cookie(
        TICKET_COOKIE,
        webclient_service.issue_ticket(settings, user.id, device.rustdesk_id),
        max_age=webclient_service.TICKET_MAX_AGE_SECONDS,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
        path=WS_PATH,
    )
    return SessionOut(
        peer_id=device.rustdesk_id,
        peer_name=device.alias or device.hostname or device.rustdesk_id,
        ws_id_path=f"{WS_PATH}/id",
        ws_relay_path=f"{WS_PATH}/relay",
        key=settings.rustdesk_key.strip(),
        my_id=f"web-{user.id}",
        my_name=user.username,
        version=__version__,
    )
