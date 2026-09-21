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

import asyncio
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


# --- checking the way through, so a failure says where it is ----------------------------------------
#
# The browser only learns that a socket closed ("ID server connection lost"). The check opens the same
# sockets from this server and, for one device, asks hbbs the question the browser asks, so the answer
# can be "hbbs is not reachable from here: connection refused" or "hbbs says the device is offline".

PROBE_TIMEOUT_SECONDS = 4.0
ANSWER_TIMEOUT_SECONDS = 6.0
CLIENT_VERSION = "1.4.0"
PUNCH_HOLE_RESPONSE = 11
RELAY_RESPONSE = 19
PUNCH_HOLE_FAILURES = {0: "ID_NOT_EXIST", 2: "OFFLINE", 3: "LICENSE_MISMATCH", 4: "LICENSE_OVERUSE"}
NAT_TYPE_SYMMETRIC = 2


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _length_delimited(number: int, payload: bytes) -> bytes:
    return _encode_varint((number << 3) | 2) + _encode_varint(len(payload)) + payload


def punch_hole_request(peer_id: str, licence_key: str, version: str = CLIENT_VERSION) -> bytes:
    """The request the web client sends to hbbs for a device: a `RendezvousMessage` holding a
    `punch_hole_request` (id, nat_type SYMMETRIC, licence_key, version, force_relay). Byte for byte what
    the client's own code produces (the tests compare them)."""
    inner = (
        _length_delimited(1, peer_id.encode())
        + _encode_varint((2 << 3) | 0)
        + _encode_varint(NAT_TYPE_SYMMETRIC)
        + _length_delimited(3, licence_key.encode())
        + _length_delimited(6, version.encode())
        + _encode_varint((8 << 3) | 0)
        + b"\x01"
    )
    return _length_delimited(PUNCH_HOLE_REQUEST, inner)


def describe_error(exc: BaseException) -> str:
    """A failure to open a WebSocket, in words."""
    import socket

    from websockets.exceptions import InvalidHandshake, InvalidStatus

    if isinstance(exc, socket.gaierror):
        return "the host name cannot be resolved"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused (nothing is listening on that port)"
    if isinstance(exc, TimeoutError):
        return f"no answer within {PROBE_TIMEOUT_SECONDS:.0f} s (a firewall may be dropping it)"
    if isinstance(exc, InvalidStatus):
        return f"it answered HTTP {exc.response.status_code}, not a WebSocket upgrade"
    if isinstance(exc, (InvalidHandshake, EOFError)):
        return "it closed the connection during the WebSocket handshake (is that a WebSocket port?)"
    if isinstance(exc, OSError):
        return f"network error ({exc.strerror or type(exc).__name__})"
    return type(exc).__name__


def _host_port(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0]


def _inner(data: bytes) -> dict[int, int | bytes]:
    found: dict[int, int | bytes] = {}
    for number, value in _fields(data):
        found.setdefault(number, value)
    return found


def classify_answer(frame: bytes) -> str:
    """What hbbs's first reply to the request means: `relay_response`, a refusal with its reason, one of
    the punch hole failures, or `other`."""
    single = _single_message(frame)
    if single is None:
        return "other"
    number, data = single
    try:
        fields = _inner(data)
    except ValueError:
        return "other"
    if number == RELAY_RESPONSE:
        reason = fields.get(6)
        if isinstance(reason, bytes) and reason:
            return f"refused: {reason.decode('utf-8', 'replace')}"
        return "relay_response"
    if number == PUNCH_HOLE_RESPONSE:
        if fields.get(1):  # a socket address: hbbs is willing to connect us
            return "relay_response"
        other = fields.get(7)
        if isinstance(other, bytes) and other:
            return f"failed: {other.decode('utf-8', 'replace')}"
        failure = fields.get(3, 0)
        return PUNCH_HOLE_FAILURES.get(failure if isinstance(failure, int) else 0, "other")
    return "other"


async def _open(url: str, client_ip: str | None):
    from websockets.asyncio.client import connect

    headers = {"X-Real-IP": client_ip} if client_ip else {}
    return await connect(
        url,
        additional_headers=headers,
        open_timeout=PROBE_TIMEOUT_SECONDS,
        ping_interval=None,
        max_size=MAX_FRAME_BYTES,
    )


async def _probe_target(url: str, client_ip: str | None) -> dict:
    try:
        ws = await _open(url, client_ip)
    except Exception as exc:  # noqa: BLE001 - any failure to connect is the answer being asked for
        return {"ok": False, "where": _host_port(url), "error": describe_error(exc)}
    await ws.close()
    return {"ok": True, "where": _host_port(url), "error": None}


async def _ask_hbbs(url: str, settings: Settings, peer_id: str, client_ip: str | None) -> str:
    """Send hbbs the browser's request for this device and classify the reply."""
    from websockets.exceptions import ConnectionClosed

    try:
        ws = await _open(url, client_ip)
    except Exception:  # noqa: BLE001 - reported by the target probe; nothing to ask
        return "unreachable"
    try:
        await ws.send(punch_hole_request(peer_id, settings.rustdesk_key.strip()))
        try:
            reply = await asyncio.wait_for(ws.recv(), ANSWER_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return "no_answer"
        except ConnectionClosed:
            return "closed"
        return classify_answer(reply) if isinstance(reply, bytes) else "other"
    finally:
        await ws.close()


async def check(settings: Settings, peer_id: str | None, client_ip: str | None) -> dict:
    hbbs_target, hbbr_target = hbbs_url(settings), hbbr_url(settings)
    hbbs, hbbr = await asyncio.gather(
        _probe_target(hbbs_target, client_ip), _probe_target(hbbr_target, client_ip)
    )
    answer = None
    if hbbs["ok"] and peer_id:
        answer = await _ask_hbbs(hbbs_target, settings, peer_id, client_ip)
    return {"hbbs": hbbs, "hbbr": hbbr, "answer": answer}


def summarise(result: dict, *, admin: bool) -> str:
    """One sentence for the person who pressed Open in browser; empty when everything is in order.
    Host names and low-level reasons are for administrators only."""
    hbbs, hbbr, answer = result["hbbs"], result["hbbr"], result["answer"]
    for name, target, setting in (
        ("ID server (hbbs)", hbbs, "WEB_CLIENT_HBBS_URL"),
        ("relay (hbbr)", hbbr, "WEB_CLIENT_HBBR_URL"),
    ):
        if not target["ok"]:
            if not admin:
                return "The server cannot reach the RustDesk servers. Tell your administrator."
            return (
                f"This server cannot open a WebSocket to the {name} at {target['where']}: {target['error']}. "
                "hbbs listens for WebSockets on port 21118 and hbbr on 21119, and they must be reachable "
                f"from this server (set {setting} if they are elsewhere)."
            )
    if answer in (None, "relay_response"):
        return ""
    if answer == "OFFLINE":
        return "The ID server says this device is offline: it has not reported to it recently."
    if answer == "ID_NOT_EXIST":
        return "The ID server does not know this device's ID."
    if answer == "LICENSE_MISMATCH":
        if admin:
            return (
                "The ID server refused the key: RUSTDESK_KEY here does not match the one hbbs was "
                "started with (the contents of its id_ed25519.pub)."
            )
        return "The ID server refused the key."
    if answer == "LICENSE_OVERUSE":
        return "The ID server refuses more connections for this key."
    if answer in ("closed", "no_answer"):
        what = (
            "closed the connection"
            if answer == "closed"
            else f"did not answer within {ANSWER_TIMEOUT_SECONDS:.0f} s"
        )
        hint = " Look at hbbs's own log for the reason." if admin else ""
        return f"The ID server {what} after the request for this device.{hint}"
    if answer.startswith(("refused:", "failed:")):
        return f"The ID server declined the request ({answer})."
    return "The ID server answered in a way the web client does not expect."
