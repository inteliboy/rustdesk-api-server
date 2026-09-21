from __future__ import annotations

import pytest
from itsdangerous import URLSafeTimedSerializer

from rustdesk_api.config import Settings
from rustdesk_api.services import webclient as wc

PUNCH_HOLE_REQUEST = bytes.fromhex(
    "42270a0931323334353637383910021a0f504c414345484f4c4445522d4b45593205312e342e304001"
)


def make(**values) -> Settings:
    values.setdefault("SECRET_KEY", "unit-test-secret")
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("id_server", "relay", "hbbs", "hbbr"),
    [
        ("rd.example.com", "", "ws://rd.example.com:21118", "ws://rd.example.com:21119"),
        ("rd.example.com:21116", "", "ws://rd.example.com:21118", "ws://rd.example.com:21119"),
        (
            "rd.example.com",
            "relay.example.com:21117",
            "ws://rd.example.com:21118",
            "ws://relay.example.com:21119",
        ),
        ("[2001:db8::1]:21116", "", "ws://[2001:db8::1]:21118", "ws://[2001:db8::1]:21119"),
        ("http://rd.example.com", "", "ws://rd.example.com:21118", "ws://rd.example.com:21119"),
        ("", "", "", ""),
    ],
)
def test_the_bridge_dials_the_hosts_the_server_is_configured_with(id_server, relay, hbbs, hbbr):
    settings = make(RUSTDESK_ID_SERVER=id_server, RUSTDESK_RELAY_SERVER=relay)
    assert wc.hbbs_url(settings) == hbbs
    assert wc.hbbr_url(settings) == hbbr


def test_explicit_urls_win_and_must_be_websocket_urls():
    settings = make(
        RUSTDESK_ID_SERVER="rd.example.com",
        WEB_CLIENT_HBBS_URL="wss://ws.example.com/id",
        WEB_CLIENT_HBBR_URL="ws://hbbr:21119",
    )
    assert wc.hbbs_url(settings) == "wss://ws.example.com/id"
    assert wc.hbbr_url(settings) == "ws://hbbr:21119"
    with pytest.raises(ValueError, match="ws://"):
        make(WEB_CLIENT_HBBS_URL="http://hbbs:21118")


def test_it_needs_a_key_and_an_id_server_and_the_switch():
    assert not wc.available(make())
    assert not wc.available(make(WEB_CLIENT_ENABLED=True, RUSTDESK_ID_SERVER="rd.example.com"))
    assert not wc.available(make(WEB_CLIENT_ENABLED=True, RUSTDESK_KEY="k"))
    assert not wc.available(make(RUSTDESK_ID_SERVER="rd.example.com", RUSTDESK_KEY="k"))
    assert wc.available(make(WEB_CLIENT_ENABLED=True, RUSTDESK_ID_SERVER="rd.example.com", RUSTDESK_KEY="k"))


def test_a_ticket_names_a_user_and_a_device_and_nothing_else_is_accepted():
    settings = make()
    token = wc.issue_ticket(settings, 7, "123456789")
    assert wc.read_ticket(settings, token) == wc.Ticket(user_id=7, peer_id="123456789")
    assert wc.read_ticket(settings, None) is None
    assert wc.read_ticket(settings, "") is None
    assert wc.read_ticket(settings, token + "x") is None
    assert wc.read_ticket(make(SECRET_KEY="another-secret"), token) is None
    # Signed with the right key but for something else (another salt, another shape).
    other_salt = URLSafeTimedSerializer("unit-test-secret", salt="something-else").dumps({"u": 7, "p": "1"})
    assert wc.read_ticket(settings, other_salt) is None
    wrong_shape = URLSafeTimedSerializer("unit-test-secret", salt=wc.TICKET_SALT).dumps(["7"])
    assert wc.read_ticket(settings, wrong_shape) is None


def test_a_ticket_expires(monkeypatch):
    settings = make()
    token = wc.issue_ticket(settings, 7, "123456789")
    monkeypatch.setattr(wc, "TICKET_MAX_AGE_SECONDS", -1)
    assert wc.read_ticket(settings, token) is None


def test_the_client_frames_are_read_the_way_the_client_wrote_them():
    assert wc.named_device(PUNCH_HOLE_REQUEST, wc.PUNCH_HOLE_REQUEST) == "123456789"
    # The same bytes are not a relay request.
    assert wc.named_device(PUNCH_HOLE_REQUEST, wc.REQUEST_RELAY) is None


@pytest.mark.parametrize(
    "frame",
    [
        b"",
        b"\x42",  # a length is missing
        b"\x42\x05\x0a\x03ab",  # the message is shorter than it says
        b"\x42\x02\x0a\xff",  # the string is longer than the message
        b"\x42\x04\x0a\x02\xff\xfe",  # not UTF-8
        b"\x42\x00",  # a message without an id
        b"\x07\x00",  # a wire type protobuf does not have
        PUNCH_HOLE_REQUEST + PUNCH_HOLE_REQUEST,  # two messages: a union has one
        PUNCH_HOLE_REQUEST + b"\x48\x01",  # one more field after it
        b"\x80" * 12,  # a varint that never ends
    ],
)
def test_anything_else_names_no_device(frame):
    assert wc.named_device(frame, wc.PUNCH_HOLE_REQUEST) is None


def test_the_session_cap_counts_relay_sockets_and_leaves_the_id_sockets_room():
    limit = wc.BridgeLimit()
    assert all(limit.acquire("relay", 2) for _ in range(2))
    assert not limit.acquire("relay", 2)
    assert limit.open_sessions() == 2
    assert all(limit.acquire("id", 2) for _ in range(4))
    assert not limit.acquire("id", 2)
    limit.release("relay")
    assert limit.acquire("relay", 2)
    for _ in range(10):
        limit.release("id")
    assert limit.acquire("id", 2)
