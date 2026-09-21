"""The browser web client: where the bridge dials, who may use it, and the tickets.

A browser cannot open the RustDesk TCP connections, but hbbs (ID server) and hbbr
(relay) also listen on WebSocket ports, 21118 and 21119. The web client speaks the
RustDesk protocol over those, and this server sits between: the browser opens
`/api/v1/webclient/ws/id` and `.../relay` here, the bridge (api/webclient_ws.py)
opens the matching socket to hbbs/hbbr and copies frames both ways.

What the bridge decides, and what it cannot:
- It lets a signed-in user through only with a short-lived ticket for one device,
  which is issued only to someone who may control that device.
- The two messages that name the device are plain protobuf (the rest of a session
  is encrypted end to end between the browser and the device), so the bridge reads
  them and refuses any other device than the ticket's.
- It cannot see or limit anything inside the encrypted stream. That is why a
  "view" share does not get a web session: view-only would be the browser's own
  setting, which the person at the keyboard could change.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from urllib.parse import urlsplit

from itsdangerous import BadData, URLSafeTimedSerializer

from rustdesk_api.config import Settings
from rustdesk_api.models.device import Device
from rustdesk_api.models.user import User
from rustdesk_api.security.permissions import can_edit_device

TICKET_SALT = "webclient-ticket"
TICKET_MAX_AGE_SECONDS = 120
HBBS_WS_PORT = 21118
HBBR_WS_PORT = 21119
# hbbs and hbbr frames are one protobuf message each; a key frame of a large screen
# is the biggest, file-transfer blocks are far smaller. Same cap as uvicorn's own.
MAX_FRAME_BYTES = 16 * 1024 * 1024

# RendezvousMessage oneof numbers (protos/rendezvous.proto).
PUNCH_HOLE_REQUEST = 8
REQUEST_RELAY = 18


def _target(configured: str, server: str, fallback_server: str, port: int) -> str:
    if configured:
        return configured
    for value in (server, fallback_server):
        value = value.strip()
        if not value:
            continue
        host = urlsplit(f"//{value}" if "//" not in value else value).hostname
        if not host:
            continue
        return f"ws://[{host}]:{port}" if ":" in host else f"ws://{host}:{port}"
    return ""


def hbbs_url(settings: Settings) -> str:
    return _target(settings.web_client_hbbs_url, settings.rustdesk_id_server, "", HBBS_WS_PORT)


def hbbr_url(settings: Settings) -> str:
    return _target(
        settings.web_client_hbbr_url,
        settings.rustdesk_relay_server,
        settings.rustdesk_id_server,
        HBBR_WS_PORT,
    )


def problems(settings: Settings) -> list[str]:
    """Why the web client cannot work as configured (empty = it can)."""
    found = []
    if not settings.rustdesk_key.strip():
        found.append("RUSTDESK_KEY is not set (the browser needs the ID server's public key).")
    if not hbbs_url(settings):
        found.append("No ID server: set RUSTDESK_ID_SERVER or WEB_CLIENT_HBBS_URL.")
    return found


def available(settings: Settings) -> bool:
    return settings.web_client_enabled and not problems(settings)


def can_connect(user: User, device: Device) -> bool:
    """Administrator, owner, or a share with "control". A "view" share is about
    seeing the device, not about taking it over."""
    return can_edit_device(user, device)


@dataclasses.dataclass(frozen=True)
class Ticket:
    user_id: int
    peer_id: str


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=TICKET_SALT)


def issue_ticket(settings: Settings, user_id: int, peer_id: str) -> str:
    return _serializer(settings).dumps({"u": user_id, "p": peer_id})


def read_ticket(settings: Settings, token: str | None) -> Ticket | None:
    if not token:
        return None
    try:
        data = _serializer(settings).loads(token, max_age=TICKET_MAX_AGE_SECONDS)
        return Ticket(user_id=int(data["u"]), peer_id=str(data["p"]))
    except (BadData, KeyError, TypeError, ValueError):
        return None


class BridgeLimit:
    """How many browser sessions are open at once (one per relay socket; the
    ID socket only lives for the first seconds of a session). In memory, per
    process, like the other limiters."""

    def __init__(self) -> None:
        self._open = {"id": 0, "relay": 0}

    def acquire(self, kind: str, limit: int) -> bool:
        # The ID sockets get twice the room: a connection attempt that is still
        # being set up must not be refused because established sessions fill the cap.
        cap = limit * 2 if kind == "id" else limit
        if self._open[kind] >= cap:
            return False
        self._open[kind] += 1
        return True

    def release(self, kind: str) -> None:
        self._open[kind] = max(0, self._open[kind] - 1)

    def open_sessions(self) -> int:
        return self._open["relay"]


# --- reading the two plain protobuf messages that name a device -----------------


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise ValueError("bad varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _fields(data: bytes) -> Iterator[tuple[int, int | bytes]]:
    """(field number, value) of a protobuf message; length-delimited values are bytes."""
    pos = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, pos = _varint(data, pos)
            yield number, value
        elif wire == 2:
            length, pos = _varint(data, pos)
            if pos + length > len(data):
                raise ValueError("truncated field")
            yield number, data[pos : pos + length]
            pos += length
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            raise ValueError("unsupported wire type")


def _single_message(frame: bytes) -> tuple[int, bytes] | None:
    """The one (oneof number, inner bytes) a RendezvousMessage carries, or None
    when it is anything else, several fields or not protobuf at all."""
    try:
        fields = list(_fields(frame))
    except ValueError:
        return None
    if len(fields) != 1:
        return None
    number, value = fields[0]
    return (number, value) if isinstance(value, bytes) else None


def named_device(frame: bytes, expected: int) -> str | None:
    """The device a `punch_hole_request` (expected=8) or `request_relay` (18)
    frame asks for; None if the frame is a different message or malformed."""
    single = _single_message(frame)
    if single is None or single[0] != expected:
        return None
    try:
        for number, value in _fields(single[1]):
            if number == 1 and isinstance(value, bytes):
                return value.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return None
