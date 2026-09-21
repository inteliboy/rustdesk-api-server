"""The browser web client: who may start a session, and the WebSocket bridge to hbbs / hbbr.

The bridge is tested against two real WebSocket servers standing in for hbbs and
hbbr. The two frames it reads are the ones the client's own code produces
(`webclient/scripts/print-frames.mjs`), for the device 123456789."""

from __future__ import annotations

import asyncio
import threading

import pytest
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

PEER = "123456789"
# What the client sends, encoded by the client (licence key and uuid are placeholders).
PUNCH_HOLE_REQUEST = bytes.fromhex(
    "42270a0931323334353637383910021a0f504c414345484f4c4445522d4b45593205312e342e304001"
)
REQUEST_RELAY = bytes.fromhex(
    "9201420a09313233343536373839"  # request_relay { id: "123456789"
    "122430303030303030302d303030302d343030302d383030302d303030303030303030303030"  # uuid
    "320f504c414345484f4c4445522d4b4559"  # licence_key: "PLACEHOLDER-KEY" }
)
WS = "/api/v1/webclient/ws"


def _frame_for(field_tag: bytes, peer: str) -> bytes:
    inner = b"\x0a" + bytes([len(peer)]) + peer.encode()
    return field_tag + bytes([len(inner)]) + inner


class FakeServer:
    """A WebSocket server that records what it receives and answers every binary frame."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.port = 0
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self.headers: list[dict[str, str]] = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(10)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()

        async def handler(connection) -> None:
            self.headers.append(dict(connection.request.headers.raw_items()))
            async for message in connection:
                if isinstance(message, bytes):
                    self.received.append(message)
                    await connection.send(b"reply:" + message)

        async with serve(handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()

    def close(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)


@pytest.fixture()
def hbbs():
    server = FakeServer()
    yield server
    server.close()


@pytest.fixture()
def hbbr():
    server = FakeServer()
    yield server
    server.close()


@pytest.fixture()
def webclient_env(monkeypatch, hbbs, hbbr) -> None:
    monkeypatch.setenv("WEB_CLIENT_ENABLED", "true")
    monkeypatch.setenv("RUSTDESK_KEY", "placeholder-public-key")
    monkeypatch.setenv("WEB_CLIENT_HBBS_URL", hbbs.url)
    monkeypatch.setenv("WEB_CLIENT_HBBR_URL", hbbr.url)


def _register(admin_client, rustdesk_id=PEER, as_user_token=None):
    token = (
        as_user_token
        or admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
            "access_token"
        ]
    )
    r = admin_client.post(
        "/api/sysinfo",
        json={"id": rustdesk_id, "hostname": "pc", "os": "windows", "version": "1.4.0"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    items = admin_client.get("/api/v1/devices").json()["items"]
    return next(d["id"] for d in items if d["rustdesk_id"] == rustdesk_id)


def _user(admin_client, name):
    admin_client.post("/api/v1/users", json={"username": name, "password": f"{name}password1"})


def _sign_in(client, name, password):
    client.post("/api/v1/auth/logout")
    client.cookies.clear()
    r = client.post("/api/v1/auth/login", json={"username": name, "password": password})
    assert r.status_code == 200
    client.headers.update({"X-CSRF-Token": client.cookies.get("rd_csrf")})


def _open_session(client, device_id):
    return client.post("/api/v1/webclient/sessions", json={"device_id": device_id})


def _cookie_header(client) -> str:
    return "; ".join(f"{name}={client.cookies.get(name)}" for name in ("rd_session", "rd_webclient"))


def _connect(client, kind, **headers):
    headers.setdefault("cookie", _cookie_header(client))
    return client.websocket_connect(f"{WS}/{kind}", headers=headers)


# --- who may start a session -----------------------------------------------------


def test_the_web_client_is_off_until_it_is_switched_on(admin_client):
    device_id = _register(admin_client)
    assert admin_client.get("/api/v1/webclient/status").json() == {
        "enabled": False,
        "available": False,
        "problems": [],
    }
    r = _open_session(admin_client, device_id)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "WEB_CLIENT_DISABLED"


def test_a_missing_key_is_reported_to_administrators_only(monkeypatch, hbbs, hbbr, admin_client):
    # (webclient_env is not used: this is the enabled-but-unset case)
    monkeypatch.setenv("WEB_CLIENT_ENABLED", "true")
    monkeypatch.setenv("WEB_CLIENT_HBBS_URL", hbbs.url)
    from rustdesk_api.config import clear_settings_cache

    clear_settings_cache()
    status = admin_client.get("/api/v1/webclient/status").json()
    assert status["enabled"] is True
    assert status["available"] is False
    assert any("RUSTDESK_KEY" in p for p in status["problems"])
    device_id = _register(admin_client)
    r = _open_session(admin_client, device_id)
    assert r.status_code == 409
    assert "RUSTDESK_KEY" in r.json()["error"]["message"]
    _user(admin_client, "bob")
    _sign_in(admin_client, "bob", "bobpassword1")
    assert admin_client.get("/api/v1/webclient/status").json()["problems"] == []


def test_an_administrator_gets_a_session_with_a_ticket_cookie(webclient_env, admin_client):
    device_id = _register(admin_client)
    r = _open_session(admin_client, device_id)
    assert r.status_code == 200
    body = r.json()
    assert body["peer_id"] == PEER
    assert body["key"] == "placeholder-public-key"
    assert body["ws_id_path"] == f"{WS}/id" and body["ws_relay_path"] == f"{WS}/relay"
    assert body["my_name"] == "admin"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("rd_webclient=") and "HttpOnly" in cookie and f"Path={WS}" in cookie
    # The ticket is in the cookie only: not in the body, and not in the audit log.
    ticket = admin_client.cookies.get("rd_webclient")
    assert ticket and ticket not in r.text
    entries = admin_client.get("/api/v1/admin/audit-logs").json()["items"]
    entry = next(e for e in entries if e["action"] == "webclient_session")
    assert entry["detail"] == {"rustdesk_id": PEER} and ticket not in str(entry)


def test_a_session_needs_the_csrf_header(webclient_env, admin_client):
    device_id = _register(admin_client)
    admin_client.headers.pop("X-CSRF-Token")
    assert _open_session(admin_client, device_id).status_code == 403


def test_a_user_reaches_only_a_device_they_own_or_may_control(webclient_env, admin_client):
    device_id = _register(admin_client)
    _user(admin_client, "bob")
    _sign_in(admin_client, "bob", "bobpassword1")
    # Not theirs: the same answer as for a device that does not exist.
    assert _open_session(admin_client, device_id).status_code == 404
    assert _open_session(admin_client, 9999).status_code == 404

    _sign_in(admin_client, "admin", "adminpass123")
    share = admin_client.post(
        f"/api/v1/devices/{device_id}/shares", json={"username": "bob", "permission": "view"}
    )
    assert share.status_code == 201
    _sign_in(admin_client, "bob", "bobpassword1")
    denied = _open_session(admin_client, device_id)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "SHARE_NOT_ALLOWED"

    _sign_in(admin_client, "admin", "adminpass123")
    admin_client.delete(f"/api/v1/devices/{device_id}/shares/{share.json()['id']}")
    admin_client.post(
        f"/api/v1/devices/{device_id}/shares", json={"username": "bob", "permission": "control"}
    )
    _sign_in(admin_client, "bob", "bobpassword1")
    assert _open_session(admin_client, device_id).status_code == 200


def test_an_api_key_cannot_start_a_session(webclient_env, admin_client):
    device_id = _register(admin_client)
    key = admin_client.post("/api/v1/api-keys", json={"label": "script", "scope": "full", "days": 30})
    assert key.status_code in (200, 201)
    token = key.json()["token"]
    r = admin_client.post(
        "/api/v1/webclient/sessions",
        json={"device_id": device_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


# --- the page and its policy -----------------------------------------------------


def test_only_the_web_client_may_compile_webassembly(client):
    page = client.get("/webclient")
    assert page.status_code == 200
    assert "wasm-unsafe-eval" in page.headers["content-security-policy"]
    assert "unsafe-eval'" not in page.headers["content-security-policy"].replace("wasm-unsafe-eval", "")
    worker = client.get("/static/webclient/session.worker.js")
    assert worker.status_code == 200 and "wasm-unsafe-eval" in worker.headers["content-security-policy"]
    assert "wasm-unsafe-eval" not in client.get("/login").headers["content-security-policy"]
    assert "wasm-unsafe-eval" not in client.get("/static/js/app.js").headers["content-security-policy"]


# --- the bridge ------------------------------------------------------------------


def test_the_id_socket_passes_the_request_for_the_ticket_device_to_hbbs(webclient_env, admin_client, hbbs):
    device_id = _register(admin_client)
    assert _open_session(admin_client, device_id).status_code == 200
    with _connect(admin_client, "id") as ws:
        ws.send_bytes(PUNCH_HOLE_REQUEST)
        assert ws.receive_bytes() == b"reply:" + PUNCH_HOLE_REQUEST
    assert hbbs.received == [PUNCH_HOLE_REQUEST]
    # hbbs is told the browser's address, as a proxy would.
    assert any(h.get("X-Real-IP") for h in hbbs.headers)


def test_the_relay_socket_pairs_then_passes_the_encrypted_stream(webclient_env, admin_client, hbbr):
    device_id = _register(admin_client)
    _open_session(admin_client, device_id)
    with _connect(admin_client, "relay") as ws:
        ws.send_bytes(REQUEST_RELAY)
        assert ws.receive_bytes() == b"reply:" + REQUEST_RELAY
        opaque = bytes(range(256))
        ws.send_bytes(opaque)
        assert ws.receive_bytes() == b"reply:" + opaque
    assert hbbr.received == [REQUEST_RELAY, opaque]


def test_a_request_for_another_device_is_refused(webclient_env, admin_client, hbbs, hbbr):
    device_id = _register(admin_client)
    _open_session(admin_client, device_id)
    other = _frame_for(b"\x42", "987654321")
    with pytest.raises(WebSocketDisconnect) as excinfo, _connect(admin_client, "id") as ws:
        ws.send_bytes(other)
        ws.receive_bytes()
    assert excinfo.value.code == 1008
    assert hbbs.received == []

    other_relay = _frame_for(b"\x92\x01", "987654321")
    with pytest.raises(WebSocketDisconnect), _connect(admin_client, "relay") as ws:
        ws.send_bytes(other_relay)
        ws.receive_bytes()
    assert hbbr.received == []


def test_the_id_socket_carries_nothing_but_that_request(webclient_env, admin_client, hbbs):
    device_id = _register(admin_client)
    _open_session(admin_client, device_id)
    # A presence query (online_request = 23) names other devices; text frames are not the protocol.
    online_request = _frame_for(b"\xba\x01", PEER)
    for frame in (online_request, "text", b"\xff\xff\xff", b""):
        with pytest.raises(WebSocketDisconnect), _connect(admin_client, "id") as ws:
            if isinstance(frame, str):
                ws.send_text(frame)
            else:
                ws.send_bytes(frame)
            ws.receive_bytes()
    assert hbbs.received == []


def test_the_bridge_needs_a_session_a_ticket_and_the_same_origin(webclient_env, admin_client, hbbs):
    device_id = _register(admin_client)
    _open_session(admin_client, device_id)
    cookie = _cookie_header(admin_client)
    session_only = f"rd_session={admin_client.cookies.get('rd_session')}"
    ticket_only = f"rd_webclient={admin_client.cookies.get('rd_webclient')}"
    for headers in (
        {"cookie": session_only},
        {"cookie": ticket_only},
        {"cookie": ""},
        {"cookie": cookie, "origin": "http://evil.example"},
        {"cookie": cookie.replace("rd_webclient=", "rd_webclient=x")},
    ):
        with pytest.raises(WebSocketDisconnect) as excinfo, _connect(admin_client, "id", **headers):
            pass
        assert excinfo.value.code == 1008
    with _connect(admin_client, "id", origin="http://testserver") as ws:
        ws.send_bytes(PUNCH_HOLE_REQUEST)
        assert ws.receive_bytes()
    assert hbbs.received == [PUNCH_HOLE_REQUEST]


def test_a_ticket_belongs_to_the_user_it_was_issued_to(webclient_env, admin_client):
    device_id = _register(admin_client)
    _open_session(admin_client, device_id)
    admins_ticket = admin_client.cookies.get("rd_webclient")
    _user(admin_client, "bob")
    _sign_in(admin_client, "bob", "bobpassword1")
    bobs_cookie = f"rd_session={admin_client.cookies.get('rd_session')}; rd_webclient={admins_ticket}"
    with pytest.raises(WebSocketDisconnect), _connect(admin_client, "id", cookie=bobs_cookie):
        pass


def test_a_share_withdrawn_after_the_ticket_was_issued_stops_the_bridge(webclient_env, admin_client):
    device_id = _register(admin_client)
    _user(admin_client, "bob")
    share = admin_client.post(
        f"/api/v1/devices/{device_id}/shares", json={"username": "bob", "permission": "control"}
    ).json()
    _sign_in(admin_client, "bob", "bobpassword1")
    assert _open_session(admin_client, device_id).status_code == 200
    bobs = {
        "rd_session": admin_client.cookies.get("rd_session"),
        "rd_webclient": admin_client.cookies.get("rd_webclient"),
    }
    _sign_in(admin_client, "admin", "adminpass123")
    admin_client.delete(f"/api/v1/devices/{device_id}/shares/{share['id']}")
    cookie = "; ".join(f"{k}={v}" for k, v in bobs.items())
    with pytest.raises(WebSocketDisconnect), _connect(admin_client, "id", cookie=cookie):
        pass


def test_an_unreachable_hbbs_closes_the_socket_with_an_error(monkeypatch, webclient_env, admin_client):
    device_id = _register(admin_client)
    _open_session(admin_client, device_id)
    # Nothing listens on this port any more.
    dead = FakeServer()
    url = dead.url
    dead.close()
    monkeypatch.setenv("WEB_CLIENT_HBBS_URL", url)
    from rustdesk_api.config import clear_settings_cache

    clear_settings_cache()
    with _connect(admin_client, "id") as ws:
        message = ws.receive()
    assert message["type"] == "websocket.close" and message["code"] == 1011


def test_the_bridge_is_closed_when_the_web_client_is_off(admin_client):
    with pytest.raises(WebSocketDisconnect):
        with admin_client.websocket_connect(f"{WS}/id"):
            pass
