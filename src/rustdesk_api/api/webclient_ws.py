"""The WebSocket bridge between the browser web client and hbbs / hbbr.

`/api/v1/webclient/ws/id` is copied to hbbs (the ID server) and `.../relay` to hbbr
(the relay); frames go both ways unchanged. See services/webclient.py for what
the bridge can and cannot decide.

A WebSocket handshake carries cookies but no CSRF header, so the same-origin check
on `Origin` (as for the live device updates) is what keeps another website from
opening one. Beyond the session, the handshake needs the ticket cookie that
POST /api/v1/webclient/sessions sets for one device.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from rustdesk_api.api.deps import SESSION_COOKIE_NAME, get_settings_dep, resolve_client_ip
from rustdesk_api.api.webclient import TICKET_COOKIE, WS_PATH
from rustdesk_api.api.ws import same_origin
from rustdesk_api.config import Settings
from rustdesk_api.db.database import session_scope
from rustdesk_api.security import network as network_policy
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import tokens as token_service
from rustdesk_api.services import webclient as webclient_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web client"])

CONNECT_TIMEOUT_SECONDS = 10

# WebSocket close codes (RFC 6455 and the IANA registry).
POLICY_VIOLATION = 1008
INTERNAL_ERROR = 1011
TRY_AGAIN_LATER = 1013


def note_problem(app, kind: str, text: str) -> None:
    """Log why a bridge did not work (never a secret) and keep the latest, which the administrator's
    connection check shows."""
    logger.warning("Web client bridge (%s): %s", kind, text)
    app.state.web_client_last = {"text": f"{kind}: {text}", "at": time.time()}


def _authorize(websocket: WebSocket, settings: Settings, kind: str) -> webclient_service.Ticket | None:
    """The ticket if this handshake may open a bridge, else None (and why, in the log). Opens a
    database session only for the check: the bridge itself lives for the whole
    remote session and must not hold a connection."""
    app = websocket.app

    def refuse(reason: str) -> None:
        note_problem(app, kind, f"refused a WebSocket before it started: {reason}")

    if not webclient_service.available(settings):
        refuse("the web client is not enabled or not set up")
        return None
    networks = settings.webui_allowed_network_list
    if networks and not network_policy.is_allowed(resolve_client_ip(websocket, settings), networks):
        refuse("the address is outside WEBUI_ALLOWED_NETWORKS")
        return None
    if not same_origin(websocket):
        refuse(
            f"the Origin ({websocket.headers.get('origin')}) is not the Host the server was addressed as "
            f"({websocket.headers.get('host')}); a reverse proxy must pass the original Host header"
        )
        return None
    ticket = webclient_service.read_ticket(settings, websocket.cookies.get(TICKET_COOKIE))
    if ticket is None:
        refuse("no valid ticket cookie (missing, expired after two minutes, or not sent to this path)")
        return None
    raw_token = websocket.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        refuse("no session cookie")
        return None
    with session_scope() as db:
        session_obj = token_service.get_valid_session(db, raw_token)
        if session_obj is None or not session_obj.user.is_active or session_obj.user_id != ticket.user_id:
            refuse("the session is not valid for this ticket")
            return None
        device = device_service.get_by_rustdesk_id(db, ticket.peer_id)
        # Checked again now, so a share withdrawn since the ticket was issued counts.
        if device is None or not webclient_service.can_connect(session_obj.user, device):
            refuse("the user may no longer control this device")
            return None
    return ticket


def _frame_allowed(kind: str, ticket: webclient_service.Ticket, data: bytes | str, first: bool) -> bool:
    """What the browser may send. The ID socket only ever carries the request for
    the ticket's device. On the relay only the first frame is readable (the
    request to be paired); after it the stream is encrypted and passes through."""
    if not isinstance(data, bytes):
        return False
    if kind == "id":
        return webclient_service.named_device(data, webclient_service.PUNCH_HOLE_REQUEST) == ticket.peer_id
    if first:
        return webclient_service.named_device(data, webclient_service.REQUEST_RELAY) == ticket.peer_id
    return True


async def _to_upstream(websocket: WebSocket, upstream, kind: str, ticket: webclient_service.Ticket) -> None:
    first = True
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is None:
            data = message.get("text")
        if data is None:
            continue
        if not _frame_allowed(kind, ticket, data, first):
            note_problem(
                websocket.app,
                kind,
                "refused a frame from the browser that is not the request for this device",
            )
            await websocket.close(code=POLICY_VIOLATION)
            return
        first = False
        await upstream.send(data)


async def _to_browser(websocket: WebSocket, upstream, counter: list[int]) -> None:
    async for message in upstream:
        counter[0] += 1
        if isinstance(message, bytes):
            await websocket.send_bytes(message)
        else:
            await websocket.send_text(message)


async def _serve(websocket: WebSocket, settings: Settings, kind: str) -> None:
    ticket = _authorize(websocket, settings, kind)
    if ticket is None:
        # Not accepted: the browser sees the handshake fail.
        await websocket.close(code=POLICY_VIOLATION)
        return

    limit = getattr(websocket.app.state, "web_client_limit", None)
    if limit is None:
        limit = webclient_service.BridgeLimit()
        websocket.app.state.web_client_limit = limit
    if not limit.acquire(kind, settings.web_client_max_sessions):
        note_problem(
            websocket.app, kind, "refused: WEB_CLIENT_MAX_SESSIONS browser sessions are already open"
        )
        await websocket.close(code=TRY_AGAIN_LATER)
        return
    try:
        await _bridge(websocket, settings, kind, ticket)
    finally:
        limit.release(kind)


async def _bridge(
    websocket: WebSocket, settings: Settings, kind: str, ticket: webclient_service.Ticket
) -> None:
    url = webclient_service.hbbs_url(settings) if kind == "id" else webclient_service.hbbr_url(settings)
    client_ip = resolve_client_ip(websocket, settings)
    # hbbs/hbbr take the peer's address from X-Real-IP when the socket comes from a proxy.
    headers = {"X-Real-IP": client_ip} if client_ip else {}
    await websocket.accept()
    started = time.monotonic()
    from_upstream = [0]
    upstream_close: list[int | None] = [None]
    try:
        # No keepalive pings of our own: hbbs and hbbr keep the socket as long as
        # it is used, and the browser and TCP notice a dead peer.
        async with connect(
            url,
            additional_headers=headers,
            max_size=webclient_service.MAX_FRAME_BYTES,
            open_timeout=CONNECT_TIMEOUT_SECONDS,
            ping_interval=None,
        ) as upstream:
            tasks = {
                asyncio.ensure_future(_to_upstream(websocket, upstream, kind, ticket)),
                asyncio.ensure_future(_to_browser(websocket, upstream, from_upstream)),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            upstream_close[0] = upstream.close_code
            for task in pending:
                task.cancel()
            for task in done | pending:
                # Either side going away ends the bridge; that is not an error.
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, ConnectionClosed):
                    await task
    except (OSError, TimeoutError, WebSocketException) as exc:
        note_problem(
            websocket.app,
            kind,
            f"could not open {_host_of(url)}: {webclient_service.describe_error(exc)}",
        )
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=INTERNAL_ERROR, reason="The RustDesk server did not answer.")
        return
    if kind == "id" and not from_upstream[0]:
        # hbbs closed its socket, or the browser did, before hbbs said anything: the client reports it as
        # "ID server connection lost".
        note_problem(
            websocket.app,
            kind,
            f"the socket to {_host_of(url)} closed after {time.monotonic() - started:.1f} s without hbbs "
            "sending anything (code "
            f"{upstream_close[0]}); see hbbs's own log",
        )
    logger.info("Web client bridge (%s) closed after %.0f s", kind, time.monotonic() - started)
    with contextlib.suppress(RuntimeError):
        await websocket.close()


def _host_of(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0]


@router.websocket(f"{WS_PATH}/id")
async def bridge_id(websocket: WebSocket, settings: Settings = Depends(get_settings_dep)) -> None:
    await _serve(websocket, settings, "id")


@router.websocket(f"{WS_PATH}/relay")
async def bridge_relay(websocket: WebSocket, settings: Settings = Depends(get_settings_dep)) -> None:
    await _serve(websocket, settings, "relay")
