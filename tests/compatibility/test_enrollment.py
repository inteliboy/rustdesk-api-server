"""`rustdesk --assign` (POST /api/devices/cli), enrollment tokens, and the
`preset-*` options a customised client puts in its sysinfo upload.

The client prints whatever text comes back and "Done!" for an empty body
(`core_main.rs`), so failures here are plain text with a 4xx status.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from rustdesk_api.config import clear_settings_cache
from rustdesk_api.security.encryption import generate_key

UUID = "M2Y5YzJhNzEtNmI0ZC00ZTA4LWE1ZDMtOWMxZTdiMjBmODQ2"


@pytest.fixture(autouse=True)
def _generous_rate_limit(monkeypatch):
    """Every request in a test comes from one IP; these tests log in and hit the
    token endpoints far more often than the default per-minute limit allows."""
    monkeypatch.setenv("AUTH_RATE_LIMIT_ATTEMPTS", "1000")
    clear_settings_cache()


def _make_token(client, label="deploy", days=30):
    r = client.post("/api/v1/enrollment-tokens", json={"label": label, "days": days})
    assert r.status_code == 201, r.text
    return r.json()


def _assign(client, token, body=None, rustdesk_id="1001", uuid=UUID):
    payload = {"id": rustdesk_id, "uuid": uuid, **(body or {})}
    return client.post("/api/devices/cli", json=payload, headers={"Authorization": f"Bearer {token}"})


def _user(app, admin_client, name):
    r = admin_client.post("/api/v1/users", json={"username": name, "password": "userpassword1"})
    assert r.status_code == 201, r.text
    other = TestClient(app)
    other.post("/api/v1/auth/login", json={"username": name, "password": "userpassword1"})
    other.headers.update({"X-CSRF-Token": other.cookies.get("rd_csrf")})
    return other, r.json()["id"]


def _device(client, rustdesk_id="1001"):
    items = client.get("/api/v1/devices").json()["items"]
    return next((d for d in items if d["rustdesk_id"] == rustdesk_id), None)


def _register(client, rustdesk_id="1001", uuid=UUID, **extra):
    r = client.post(
        "/api/sysinfo", json={"id": rustdesk_id, "uuid": uuid, "hostname": "h", "os": "linux / x", **extra}
    )
    assert r.status_code == 200
    return r


def _personal_peers(client):
    """The admin's personal address book, read the way the client does."""
    token = client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    r = client.get("/api/ab", headers={"Authorization": f"Bearer {token}"})
    return json.loads(r.json()["data"])["peers"]


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------


def test_a_token_is_shown_once_and_only_a_hash_is_stored(admin_client):
    from sqlalchemy import text

    from rustdesk_api.db.database import get_session_factory

    created = _make_token(admin_client, "installer")
    assert created["token"] and created["label"] == "installer"

    listed = admin_client.get("/api/v1/enrollment-tokens").json()
    assert [t["label"] for t in listed] == ["installer"]
    assert "token" not in listed[0]

    with get_session_factory()() as db:
        stored = [row[0] for row in db.execute(text("SELECT token_hash FROM auth_sessions")).all()]
    assert created["token"] not in stored


def test_an_enrollment_token_is_useless_anywhere_else(client, admin_client):
    token = _make_token(admin_client)["token"]
    headers = {"Authorization": f"Bearer {token}"}
    fresh = TestClient(client.app)
    assert fresh.get("/api/v1/auth/me", headers=headers).status_code == 401
    assert fresh.get("/api/v1/devices", headers=headers).status_code == 401
    assert fresh.post("/api/currentUser", json={}, headers=headers).json().get("error")

    # The address book answers an unrecognised token like an anonymous caller
    # (an empty book), not with the token owner's entries.
    login = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"})
    real = {"Authorization": f"Bearer {login.json()['access_token']}"}
    book = {"tags": [], "peers": [{"id": "9", "alias": "secret peer"}], "tag_colors": "{}"}
    assert fresh.post("/api/ab", json={"data": json.dumps(book)}, headers=real).status_code == 200
    assert "secret peer" in fresh.get("/api/ab", headers=real).text
    assert "secret peer" not in fresh.get("/api/ab", headers=headers).text


def test_tokens_can_be_revoked_and_only_by_their_owner(app, admin_client):
    alice, _ = _user(app, admin_client, "alice")
    mine = _make_token(admin_client)
    theirs = _make_token(alice)

    assert admin_client.delete(f"/api/v1/enrollment-tokens/{theirs['id']}").status_code == 404
    assert alice.delete(f"/api/v1/enrollment-tokens/{mine['id']}").status_code == 404

    assert _assign(admin_client, mine["token"], {"note": "x"}).status_code == 200
    assert admin_client.delete(f"/api/v1/enrollment-tokens/{mine['id']}").status_code == 204
    r = _assign(admin_client, mine["token"], {"note": "y"})
    assert r.status_code == 401
    assert admin_client.get("/api/v1/enrollment-tokens").json() == []


def test_token_lifetime_is_bounded(admin_client):
    for days in (0, 366):
        r = admin_client.post("/api/v1/enrollment-tokens", json={"label": "x", "days": days})
        assert r.status_code == 422
    assert admin_client.post("/api/v1/enrollment-tokens", json={"label": "", "days": 5}).status_code == 422


def test_creating_and_revoking_tokens_is_audited_without_the_token(admin_client):
    created = _make_token(admin_client, "ci")
    admin_client.delete(f"/api/v1/enrollment-tokens/{created['id']}")
    items = admin_client.get("/api/v1/admin/audit-logs").json()["items"]
    actions = [i["action"] for i in items]
    assert "enrollment_token_created" in actions and "enrollment_token_revoked" in actions
    assert created["token"] not in str(items)


# --------------------------------------------------------------------------
# --assign
# --------------------------------------------------------------------------


def test_no_token_or_a_bad_one_is_refused_in_plain_text(client):
    r = client.post("/api/devices/cli", json={"id": "1", "note": "x"})
    assert (r.status_code, r.headers["content-type"].startswith("text/plain")) == (401, True)
    r = client.post(
        "/api/devices/cli", json={"id": "1", "note": "x"}, headers={"Authorization": "Bearer nope"}
    )
    assert r.status_code == 401
    assert client.get("/api/v1/devices").status_code == 401


def test_success_is_an_empty_200_which_the_client_reports_as_done(admin_client):
    token = _make_token(admin_client)["token"]
    r = _assign(admin_client, token, {"note": "Front desk", "device_name": "Reception PC"})
    assert (r.status_code, r.content) == (200, b"")
    device = _device(admin_client)
    assert (device["note"], device["alias"]) == ("Front desk", "Reception PC")


def test_an_unknown_device_is_registered_by_the_command(admin_client):
    token = _make_token(admin_client)["token"]
    assert _device(admin_client) is None
    assert _assign(admin_client, token, {"note": "n"}).status_code == 200
    # ... and its later sysinfo upload fills in the rest without duplicating it,
    # and is recognised as the same install (the uuid was recorded).
    _register(admin_client)
    assert len(admin_client.get("/api/v1/devices").json()["items"]) == 1
    assert _device(admin_client)["uuid_change_pending"] is False
    assert "uuid" not in _device(admin_client)


def test_a_different_machine_cannot_claim_a_registered_id(admin_client):
    token = _make_token(admin_client)["token"]
    _register(admin_client)
    r = _assign(admin_client, token, {"note": "x"}, uuid="some-other-machine")
    assert r.status_code == 409
    assert _device(admin_client)["note"] is None


def test_nothing_to_assign_and_bad_input_are_errors(admin_client):
    token = _make_token(admin_client)["token"]
    assert _assign(admin_client, token, {}).status_code == 400
    assert _assign(admin_client, token, {"note": "   "}).status_code == 400
    assert _assign(admin_client, token, {"note": "x"}, rustdesk_id="").status_code == 400
    assert _assign(admin_client, token, {"note": "x"}, rustdesk_id="x" * 65).status_code == 400
    r = admin_client.post(
        "/api/devices/cli", json={"id": "1", "note": 5}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 400  # a non-text value counts as absent


def test_an_administrator_can_assign_owner_group_and_strategy(admin_client, app):
    _, alice_id = _user(app, admin_client, "alice")
    token = _make_token(admin_client)["token"]
    _register(admin_client)
    admin_client.post("/api/v1/groups", json={"name": "Servers"})
    admin_client.post("/api/v1/strategies", json={"name": "Kiosk", "options": {"enable-audio": "N"}})

    # The group belongs to whoever the device is being given to, here alice: not found.
    r = _assign(admin_client, token, {"user_name": "alice", "device_group_name": "Servers"})
    assert (r.status_code, "no device group" in r.text) == (400, True)
    assert _device(admin_client)["owner_id"] != alice_id  # nothing was applied

    r = _assign(
        admin_client, token, {"user_name": "admin", "device_group_name": "servers", "strategy_name": "kiosk"}
    )
    assert r.status_code == 200, r.text
    device = _device(admin_client)
    assert (device["group_name"], device["strategy_name"]) == ("Servers", "Kiosk")

    r = _assign(admin_client, token, {"user_name": "ALICE"})
    assert r.status_code == 200
    assert _device(admin_client)["owner_username"] == "alice"


def test_a_missing_user_group_book_or_strategy_applies_nothing(admin_client):
    token = _make_token(admin_client)["token"]
    _register(admin_client)
    for body, needle in (
        ({"note": "x", "user_name": "ghost"}, "was not found"),
        ({"note": "x", "device_group_name": "Nope"}, "no device group"),
        ({"note": "x", "address_book_name": "Nope"}, "no address book"),
        ({"note": "x", "strategy_name": "Nope"}, "no strategy"),
        ({"note": "x", "device_username": "root"}, "not supported"),
    ):
        r = _assign(admin_client, token, body)
        assert r.status_code == 400 and needle in r.text, (body, r.text)
    assert _device(admin_client)["note"] is None


def test_the_personal_address_book_gets_an_entry_with_a_tag(admin_client):
    token = _make_token(admin_client)["token"]
    _register(admin_client)
    r = _assign(
        admin_client,
        token,
        {
            "address_book_name": "My address book",
            "address_book_tag": "office",
            "address_book_alias": "Reception",
            "address_book_note": "front desk",
        },
    )
    assert r.status_code == 200, r.text
    (peer,) = _personal_peers(admin_client)
    assert (peer["id"], peer["alias"], peer["tags"]) == ("1001", "Reception", ["office"])
    assert peer["hostname"] == "h"

    # Running it again updates in place and does not blank what it did not mention.
    assert (
        _assign(
            admin_client, token, {"address_book_name": "my ADDRESS book", "address_book_tag": "lab"}
        ).status_code
        == 200
    )
    (peer,) = _personal_peers(admin_client)
    assert (peer["alias"], peer["tags"]) == ("Reception", ["lab"])


def test_a_shared_address_book_takes_a_saved_password_only_with_a_key(admin_client, monkeypatch):
    token = _make_token(admin_client)["token"]
    _register(admin_client)
    admin_client.post("/api/v1/address-books", json={"name": "Team"})

    r = _assign(admin_client, token, {"address_book_name": "Team", "address_book_password": "s3cret-pw"})
    assert r.status_code == 400 and "DATA_ENCRYPTION_KEY" in r.text
    r = _assign(admin_client, token, {"address_book_name": "My address book", "address_book_password": "x"})
    assert r.status_code == 400 and "shared address book" in r.text

    monkeypatch.setenv("DATA_ENCRYPTION_KEY", generate_key())
    clear_settings_cache()
    r = _assign(admin_client, token, {"address_book_name": "Team", "address_book_password": "s3cret-pw"})
    assert r.status_code == 200, r.text
    # Neither the response nor the audit trail ever carries the password.
    assert "s3cret-pw" not in r.text
    assert "s3cret-pw" not in str(admin_client.get("/api/v1/admin/audit-logs").json())
    book = admin_client.get("/api/v1/address-books").json()
    assert next(b for b in book if b["name"] == "Team")["entry_count"] == 1


def test_a_user_can_only_touch_their_own_things(app, admin_client):
    alice, alice_id = _user(app, admin_client, "alice")
    bob, _ = _user(app, admin_client, "bob")
    alice_token = _make_token(alice)["token"]
    admin_client.post("/api/v1/strategies", json={"name": "Kiosk", "options": {}})
    _register(admin_client)  # unowned

    # An unowned device can be given a note; owner stays as it was.
    assert _assign(alice, alice_token, {"note": "mine now?"}).status_code == 200

    # Not to someone else, and no strategies.
    r = _assign(alice, alice_token, {"user_name": "bob"})
    assert (r.status_code, "administrator" in r.text) == (400, True)
    r = _assign(alice, alice_token, {"strategy_name": "Kiosk"})
    assert (r.status_code, "administrator" in r.text) == (400, True)

    # Herself, yes.
    assert _assign(alice, alice_token, {"user_name": "alice"}).status_code == 200
    assert _device(alice)["owner_id"] == alice_id

    # Bob cannot touch alice's device with his own token.
    bob_token = _make_token(bob)["token"]
    r = _assign(bob, bob_token, {"note": "hijack"})
    assert (r.status_code, "another user" in r.text) == (400, True)
    assert _device(alice)["note"] == "mine now?"

    # ... nor put it in somebody else's address book.
    admin_client.post("/api/v1/address-books", json={"name": "Admin only"})
    r = _assign(alice, alice_token, {"address_book_name": "Admin only"})
    assert r.status_code == 400 and "no address book" in r.text


def test_a_regular_login_token_also_works_for_assign(app, admin_client):
    admin_login = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"})
    token = admin_login.json()["access_token"]
    assert _assign(admin_client, token, {"note": "via login token"}).status_code == 200


def test_assignments_are_audited_without_secrets(admin_client):
    token = _make_token(admin_client)["token"]
    _assign(admin_client, token, {"note": "hello", "device_name": "Box"})
    entry = next(
        i
        for i in admin_client.get("/api/v1/admin/audit-logs").json()["items"]
        if i["action"] == "device_assigned"
    )
    assert entry["detail"]["note"] == "hello" and entry["detail"]["via"] == "rustdesk_cli"
    assert token not in str(entry)


def test_a_deactivated_users_token_stops_working(admin_client, app):
    alice, alice_id = _user(app, admin_client, "alice")
    token = _make_token(alice)["token"]
    assert _assign(alice, token, {"note": "x"}).status_code == 200
    admin_client.patch(f"/api/v1/users/{alice_id}", json={"is_active": False})
    assert _assign(alice, token, {"note": "y"}).status_code == 401


# --------------------------------------------------------------------------
# preset-* keys in sysinfo
# --------------------------------------------------------------------------

PRESETS = {
    "preset-user-name": "admin",
    "preset-address-book-name": "My address book",
    "preset-address-book-tag": "kiosk",
    "preset-address-book-alias": "Lobby",
    "preset-device-group-name": "Servers",
    "preset-note": "from a preset",
    "preset-strategy-name": "Kiosk",
}


@pytest.fixture()
def presets_enabled(monkeypatch):
    monkeypatch.setenv("ALLOW_SYSINFO_PRESETS", "true")
    clear_settings_cache()


def _prepare_targets(admin_client):
    admin_client.post("/api/v1/groups", json={"name": "Servers"})
    admin_client.post("/api/v1/strategies", json={"name": "Kiosk", "options": {}})


def test_presets_are_ignored_unless_enabled(admin_client):
    _prepare_targets(admin_client)
    _register(admin_client, **PRESETS)
    device = _device(admin_client)
    assert (device["owner_id"], device["note"], device["group_id"], device["strategy_id"]) == (None,) * 4
    assert _personal_peers(admin_client) == []


def test_presets_place_a_new_device(admin_client, presets_enabled):
    _prepare_targets(admin_client)
    r = _register(admin_client, **PRESETS)
    assert r.text == "SYSINFO_UPDATED"
    device = _device(admin_client)
    assert device["owner_username"] == "admin"
    assert (device["note"], device["group_name"], device["strategy_name"]) == (
        "from a preset",
        "Servers",
        "Kiosk",
    )
    (peer,) = _personal_peers(admin_client)
    assert (peer["alias"], peer["tags"]) == ("Lobby", ["kiosk"])
    entry = next(
        i
        for i in admin_client.get("/api/v1/admin/audit-logs").json()["items"]
        if i["action"] == "device_preset_applied"
    )
    assert entry["detail"]["via"] == "sysinfo"


def test_presets_never_rearrange_a_device_that_already_exists(admin_client, presets_enabled):
    """The upload has no credentials: only a device's first registration is
    affected, so knowing an id is not enough to move an existing device."""
    _prepare_targets(admin_client)
    _register(admin_client)
    _register(admin_client, **PRESETS)
    device = _device(admin_client)
    assert (device["owner_id"], device["note"], device["group_id"]) == (None, None, None)


def test_a_preset_that_cannot_be_honoured_does_not_stop_registration(admin_client, presets_enabled):
    r = _register(admin_client, **{**PRESETS, "preset-user-name": "ghost"})
    assert (r.status_code, r.text) == (200, "SYSINFO_UPDATED")
    device = _device(admin_client)
    assert device is not None and device["owner_id"] is None and device["note"] is None

    r = _register(
        admin_client, rustdesk_id="2002", **{"preset-address-book-name": "Nope", "preset-note": "kept?"}
    )
    assert r.text == "SYSINFO_UPDATED" and _device(admin_client, "2002") is not None


def test_preset_values_that_are_not_text_are_ignored(admin_client, presets_enabled):
    r = _register(
        admin_client, **{"preset-note": ["x"], "preset-user-name": 5, "preset-strategy-name": {"a": 1}}
    )
    assert r.text == "SYSINFO_UPDATED"
    assert _device(admin_client)["note"] is None
