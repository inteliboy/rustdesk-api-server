"""WebSocket endpoint backing the WebUI's live device-status updates
(see rustdesk_api.ws for the connection manager/broadcast logic).

Auth: a WebSocket handshake carries cookies like an ordinary GET request,
but browsers do not enforce CORS on it the way they do fetch/XHR - so
cookie-only auth here is a cross-site WebSocket hijacking risk unless
mitigated (CLAUDE.md section 25: CSRF protection must not be skipped just
because cookie auth makes it inconvenient). Browsers cannot attach a
custom Authorization header or CSRF header to a WebSocket handshake from
JS, so the mitigation here is a same-origin check on the `Origin` header,
which browsers set automatically and page JS cannot spoof.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import SESSION_COOKIE_NAME, get_settings_dep, resolve_client_ip
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.security import network as network_policy
from rustdesk_api.services import tokens as token_service

router = APIRouter(tags=["ws"])


def same_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if origin is None or host is None:
        # No Origin header at all (e.g. a non-browser client) - nothing to
        # cross-check, so this specific mitigation doesn't apply; session
        # auth below still gates the connection.
        return True
    origin_host = origin.split("://", 1)[-1]
    return origin_host == host


@router.websocket("/api/v1/ws/devices")
async def devices_ws(
    websocket: WebSocket, db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep)
) -> None:
    # The HTTP middleware that enforces WEBUI_ALLOWED_NETWORKS does not see
    # WebSocket connections, so the same rule is applied here.
    networks = settings.webui_allowed_network_list
    if networks and not network_policy.is_allowed(resolve_client_ip(websocket, settings), networks):
        await websocket.close(code=1008)
        return
    if not same_origin(websocket):
        await websocket.close(code=1008)
        return

    raw_token = websocket.cookies.get(SESSION_COOKIE_NAME)
    session_obj = token_service.get_valid_session(db, raw_token) if raw_token else None
    if session_obj is None or not session_obj.user.is_active:
        await websocket.close(code=1008)
        return

    user = session_obj.user
    manager = websocket.app.state.ws_device_manager
    await manager.connect(websocket, user_id=user.id, is_admin=user.is_admin)
    try:
        while True:
            # No client->server messages are expected; this just blocks
            # until the browser closes the connection (tab closed,
            # navigation, etc).
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)
