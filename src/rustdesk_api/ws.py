"""Live device-status updates for the WebUI (CLAUDE.md section 47).

In-memory only, scoped to a single running application instance
(`app.state.ws_device_manager`) - matches the existing RateLimiter pattern
(security/rate_limit.py) and CLAUDE.md section 26/41's "do not introduce
Redis as a mandatory dependency" for a single small self-hosted
deployment. If this server is ever run with multiple worker processes,
each process only sees the connections (and only broadcasts the device
changes) it handles itself - documented limitation, not a bug to silently
paper over.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import WebSocket

from rustdesk_api.models.device import Device

logger = logging.getLogger(__name__)


@dataclass
class _Connection:
    websocket: WebSocket
    user_id: int
    is_admin: bool


class DeviceConnectionManager:
    def __init__(self) -> None:
        self._connections: list[_Connection] = []

    async def connect(self, websocket: WebSocket, *, user_id: int, is_admin: bool) -> None:
        await websocket.accept()
        self._connections.append(_Connection(websocket, user_id, is_admin))

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections = [c for c in self._connections if c.websocket is not websocket]

    async def notify_device_change(
        self, *, device_owner_id: int | None, shared_with_user_ids: set[int], payload: dict
    ) -> None:
        """Only sent to connections that could actually view this device
        (its owner, admins, or a user it's shared with) - a device's
        existence and identity must not leak to anyone else, same rule as
        the REST API (CLAUDE.md section 65: IDOR)."""
        if not self._connections:
            return
        stale: list[_Connection] = []
        for conn in list(self._connections):
            visible = conn.is_admin or conn.user_id == device_owner_id or conn.user_id in shared_with_user_ids
            if not visible:
                continue
            try:
                await conn.websocket.send_json(payload)
            except Exception:
                stale.append(conn)
        for conn in stale:
            self.disconnect(conn.websocket)


def device_snapshot_for_broadcast(
    device: Device, online_timeout_seconds: int
) -> tuple[dict, int | None, set[int]]:
    """Extracts everything notify_device_change needs from a Device ORM
    object while its session is still open. Broadcasting is scheduled as a
    FastAPI BackgroundTask, which runs after the request's DB session has
    been closed - a plain dict/primitives snapshot avoids ever touching a
    detached ORM instance from that callback."""
    shared_with_user_ids = {share.shared_with_user_id for share in device.shares if share.is_active()}
    payload = {
        "type": "device_updated",
        "device": {
            "id": device.id,
            "rustdesk_id": device.rustdesk_id,
            "hostname": device.hostname,
            "alias": device.alias,
            "last_seen": device.last_seen.isoformat() if device.last_seen else None,
            "online": device.is_online(online_timeout_seconds),
        },
    }
    return payload, device.owner_id, shared_with_user_ids
