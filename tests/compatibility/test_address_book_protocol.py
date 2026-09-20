"""RustDesk client address book, newer per-item protocol.

Every request here mirrors what `flutter/lib/models/ab_model.dart` sends
(verified against the client source 2026-09-20; not yet against a live
client): reads are POSTs with an empty body, writes must succeed with a 200
and an *empty* body, errors are `{"error": "<text>"}`.
"""

import json

import pytest

from rustdesk_api.config import clear_settings_cache

PASSWORD = "alicepassword1"


def _login(client, username="admin", password="adminpass123"):
    r = client.post("/api/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _make_user(admin_client, username):
    r = admin_client.post(
        "/api/v1/users", json={"username": username, "password": PASSWORD, "is_admin": False}
    )
    assert r.status_code == 201
    return _login(admin_client, username, PASSWORD)


def _personal_guid(client, headers):
    return client.post("/api/ab/personal", headers=headers).json()["guid"]


def _new_book(admin_client, name="Team"):
    """A shared book owned by admin (via the WebUI API); returns its guid."""
    r = admin_client.post("/api/v1/address-books", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["guid"]


def _share(admin_client, guid, username, rule):
    r = admin_client.put(f"/api/v1/address-books/{guid}/shares", json={"username": username, "rule": rule})
    assert r.status_code == 200, r.text


def _peers(client, headers, guid, **params):
    r = client.post(
        "/api/ab/peers", params={"ab": guid, "current": 1, "pageSize": 100, **params}, headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()


def _add(client, headers, guid, peer):
    return client.post(f"/api/ab/peer/add/{guid}", json=peer, headers=headers)


# -- mode discovery -----------------------------------------------------------


def test_personal_returns_a_stable_guid(admin_client):
    headers = _login(admin_client)
    first = admin_client.post("/api/ab/personal", headers=headers)
    assert first.status_code == 200
    guid = first.json()["guid"]
    assert len(guid) == 36
    assert admin_client.post("/api/ab/personal", headers=headers).json() == {"guid": guid}


def test_settings_reports_no_peer_limit(admin_client):
    r = admin_client.post("/api/ab/settings", headers=_login(admin_client))
    assert r.status_code == 200
    assert r.json() == {"max_peer_one_ab": 0}


def test_legacy_mode_answers_404_so_the_client_falls_back(admin_client, monkeypatch):
    """`ADDRESS_BOOK_LEGACY_MODE=true`: the client sees a 404 on /api/ab/personal
    ("current api server is legacy mode") and never asks for the rest."""
    headers = _login(admin_client)
    monkeypatch.setenv("ADDRESS_BOOK_LEGACY_MODE", "true")
    clear_settings_cache()
    try:
        for path in ("/api/ab/personal", "/api/ab/settings", "/api/ab/shared/profiles"):
            r = admin_client.post(path, headers=headers)
            assert r.status_code == 404, path
            assert "error" in r.json()
        # ... while the legacy endpoints keep working.
        assert admin_client.get("/api/ab", headers=headers).status_code == 200
    finally:
        monkeypatch.delenv("ADDRESS_BOOK_LEGACY_MODE")
        clear_settings_cache()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/ab/personal"),
        ("post", "/api/ab/settings"),
        ("post", "/api/ab/shared/profiles"),
        ("post", "/api/ab/peers?ab=x"),
        ("post", "/api/ab/tags/x"),
        ("post", "/api/ab/peer/add/x"),
        ("put", "/api/ab/peer/update/x"),
        ("delete", "/api/ab/peer/x"),
        ("post", "/api/ab/tag/add/x"),
        ("put", "/api/ab/tag/rename/x"),
        ("put", "/api/ab/tag/update/x"),
        ("delete", "/api/ab/tag/x"),
    ],
)
def test_every_endpoint_requires_a_token(client, method, path):
    r = getattr(client, method)(path)
    assert r.status_code == 401
    assert r.json() == {"error": "Not authenticated."}


# -- peers --------------------------------------------------------------------


def test_add_peer_succeeds_with_an_empty_200_body(admin_client):
    """The client treats any non-empty body as an error - even `{}` would show
    up as the error text "null"."""
    headers = _login(admin_client)
    guid = _personal_guid(admin_client, headers)
    r = _add(admin_client, headers, guid, {"id": "111", "alias": "a", "hash": "h", "tags": []})
    assert r.status_code == 200
    assert r.content == b""


def test_peers_are_paginated_in_the_clients_envelope(admin_client):
    headers = _login(admin_client)
    guid = _personal_guid(admin_client, headers)
    for i in range(5):
        _add(admin_client, headers, guid, {"id": f"10{i}", "alias": f"a{i}"})

    page1 = _peers(admin_client, headers, guid, pageSize=2, current=1)
    page3 = _peers(admin_client, headers, guid, pageSize=2, current=3)
    assert page1["total"] == 5
    assert [p["id"] for p in page1["data"]] == ["100", "101"]
    assert [p["id"] for p in page3["data"]] == ["104"]


def test_personal_peer_round_trips_the_fields_the_client_reads(admin_client):
    headers = _login(admin_client)
    guid = _personal_guid(admin_client, headers)
    _add(
        admin_client,
        headers,
        guid,
        {
            "id": "111",
            "username": "bob",
            "hostname": "box",
            "platform": "Windows",
            "alias": "office",
            "tags": ["work"],
            "hash": "opaque",
        },
    )
    peer = _peers(admin_client, headers, guid)["data"][0]
    assert peer == {
        "id": "111",
        "username": "bob",
        "hostname": "box",
        "platform": "Windows",
        "alias": "office",
        "tags": ["work"],
        "note": "",
        "hash": "opaque",
    }


def test_add_is_idempotent(admin_client):
    headers = _login(admin_client)
    guid = _personal_guid(admin_client, headers)
    _add(admin_client, headers, guid, {"id": "111", "alias": "first"})
    assert _add(admin_client, headers, guid, {"id": "111", "alias": "second"}).status_code == 200
    data = _peers(admin_client, headers, guid)
    assert data["total"] == 1
    assert data["data"][0]["alias"] == "second"


def test_update_changes_only_the_fields_that_were_sent(admin_client):
    headers = _login(admin_client)
    guid = _personal_guid(admin_client, headers)
    _add(
        admin_client,
        headers,
        guid,
        {"id": "111", "alias": "keep", "hostname": "h", "tags": ["t1"], "hash": "x"},
    )

    r = admin_client.put(f"/api/ab/peer/update/{guid}", json={"id": "111", "note": "hello"}, headers=headers)
    assert r.status_code == 200
    assert r.content == b""
    peer = _peers(admin_client, headers, guid)["data"][0]
    assert (peer["note"], peer["alias"], peer["hostname"], peer["tags"], peer["hash"]) == (
        "hello",
        "keep",
        "h",
        ["t1"],
        "x",
    )

    admin_client.put(f"/api/ab/peer/update/{guid}", json={"id": "111", "tags": []}, headers=headers)
    assert _peers(admin_client, headers, guid)["data"][0]["tags"] == []


def test_update_of_an_unknown_peer_is_an_error_the_client_can_show(admin_client):
    headers = _login(admin_client)
    guid = _personal_guid(admin_client, headers)
    r = admin_client.put(f"/api/ab/peer/update/{guid}", json={"id": "nope", "alias": "x"}, headers=headers)
    assert r.status_code == 404
    assert r.json() == {"error": "That peer is not in this address book."}


def test_delete_peers_takes_a_json_list_of_ids(admin_client):
    headers = _login(admin_client)
    guid = _personal_guid(admin_client, headers)
    for rid in ("1", "2", "3"):
        _add(admin_client, headers, guid, {"id": rid})
    r = admin_client.request(
        "DELETE", f"/api/ab/peer/{guid}", content=json.dumps(["1", "3"]), headers=headers
    )
    assert r.status_code == 200
    assert r.content == b""
    assert [p["id"] for p in _peers(admin_client, headers, guid)["data"]] == ["2"]


def test_a_bad_body_is_reported_not_a_500(admin_client):
    headers = _login(admin_client)
    guid = _personal_guid(admin_client, headers)
    assert (
        admin_client.post(f"/api/ab/peer/add/{guid}", content=b"not json", headers=headers).status_code == 422
    )
    assert _add(admin_client, headers, guid, {"alias": "no id"}).status_code == 422
    assert _add(admin_client, headers, guid, {"id": "x" * 65}).status_code == 422
    r = admin_client.request("DELETE", f"/api/ab/peer/{guid}", content=json.dumps({"a": 1}), headers=headers)
    assert r.status_code == 422


def test_a_shared_books_password_is_accepted_but_not_stored_without_a_key(admin_client):
    guid = _new_book(admin_client)
    headers = _login(admin_client)
    _add(admin_client, headers, guid, {"id": "111", "alias": "srv", "password": "secret-pw", "hash": "h"})
    admin_client.put(
        f"/api/ab/peer/update/{guid}", json={"id": "111", "password": "secret-pw"}, headers=headers
    )

    peer = _peers(admin_client, headers, guid)["data"][0]
    assert "hash" not in peer
    assert "password" not in peer
    assert "secret-pw" not in json.dumps(admin_client.get(f"/api/v1/address-book?book={guid}").json())


# -- tags ---------------------------------------------------------------------


def test_tag_lifecycle(admin_client):
    headers = _login(admin_client)
    guid = _personal_guid(admin_client, headers)
    _add(admin_client, headers, guid, {"id": "111", "tags": ["old"]})

    r = admin_client.post(
        f"/api/ab/tag/add/{guid}", json={"name": "blue", "color": 0xFF0000FF}, headers=headers
    )
    assert (r.status_code, r.content) == (200, b"")
    tags = admin_client.post(f"/api/ab/tags/{guid}", headers=headers).json()
    assert {"name": "blue", "color": 0xFF0000FF} in tags
    assert all(isinstance(t["color"], int) for t in tags)

    admin_client.put(
        f"/api/ab/tag/update/{guid}", json={"name": "blue", "color": 0xFF00FF00}, headers=headers
    )
    assert {"name": "blue", "color": 0xFF00FF00} in admin_client.post(
        f"/api/ab/tags/{guid}", headers=headers
    ).json()

    r = admin_client.put(f"/api/ab/tag/rename/{guid}", json={"old": "old", "new": "newer"}, headers=headers)
    assert r.status_code == 200
    assert _peers(admin_client, headers, guid)["data"][0]["tags"] == ["newer"]

    r = admin_client.request("DELETE", f"/api/ab/tag/{guid}", content=json.dumps(["newer"]), headers=headers)
    assert r.status_code == 200
    assert _peers(admin_client, headers, guid)["data"][0]["tags"] == []
    assert [t["name"] for t in admin_client.post(f"/api/ab/tags/{guid}", headers=headers).json()] == ["blue"]


def test_renaming_onto_an_existing_tag_is_refused(admin_client):
    headers = _login(admin_client)
    guid = _personal_guid(admin_client, headers)
    for name in ("a", "b"):
        admin_client.post(f"/api/ab/tag/add/{guid}", json={"name": name, "color": 1}, headers=headers)
    r = admin_client.put(f"/api/ab/tag/rename/{guid}", json={"old": "a", "new": "b"}, headers=headers)
    assert r.status_code == 409
    assert "already exists" in r.json()["error"]


def test_adding_an_existing_tag_just_recolors_it(admin_client):
    headers = _login(admin_client)
    guid = _personal_guid(admin_client, headers)
    for color in (0xFF111111, 0xFF222222):
        admin_client.post(f"/api/ab/tag/add/{guid}", json={"name": "t", "color": color}, headers=headers)
    assert admin_client.post(f"/api/ab/tags/{guid}", headers=headers).json() == [
        {"name": "t", "color": 0xFF222222}
    ]


# -- the same personal book, in both protocols ----------------------------------


def test_legacy_and_newer_protocols_share_the_personal_book(admin_client):
    headers = _login(admin_client)
    guid = _personal_guid(admin_client, headers)
    _add(admin_client, headers, guid, {"id": "new-1", "alias": "via new", "tags": ["x"]})

    legacy = json.loads(admin_client.get("/api/ab", headers=headers).json()["data"])
    assert [p["id"] for p in legacy["peers"]] == ["new-1"]
    assert legacy["tags"] == ["x"]

    inner = {"tags": [], "peers": [{"id": "legacy-1", "alias": "via legacy", "tags": []}], "tag_colors": "{}"}
    admin_client.post("/api/ab", json={"data": json.dumps(inner)}, headers=headers)
    assert [p["id"] for p in _peers(admin_client, headers, guid)["data"]] == ["legacy-1"]


def test_a_legacy_push_keeps_a_note_set_by_the_newer_protocol(admin_client):
    headers = _login(admin_client)
    guid = _personal_guid(admin_client, headers)
    _add(admin_client, headers, guid, {"id": "111", "note": "remember"})
    inner = {"tags": [], "peers": [{"id": "111", "alias": "renamed", "tags": []}], "tag_colors": "{}"}
    admin_client.post("/api/ab", json={"data": json.dumps(inner)}, headers=headers)
    peer = _peers(admin_client, headers, guid)["data"][0]
    assert (peer["alias"], peer["note"]) == ("renamed", "remember")


# -- authorization ------------------------------------------------------------


def test_another_users_personal_book_is_not_found_by_guid(admin_client):
    admin_headers = _login(admin_client)
    admin_guid = _personal_guid(admin_client, admin_headers)
    _add(admin_client, admin_headers, admin_guid, {"id": "secret-peer"})
    alice = _make_user(admin_client, "alice")

    # Same answer as for a guid that does not exist at all.
    assert admin_client.post("/api/ab/peers", params={"ab": admin_guid}, headers=alice).status_code == 404
    assert admin_client.post(f"/api/ab/tags/{admin_guid}", headers=alice).status_code == 404
    assert _add(admin_client, alice, admin_guid, {"id": "planted"}).status_code == 404
    r = admin_client.put(f"/api/ab/peer/update/{admin_guid}", json={"id": "secret-peer"}, headers=alice)
    assert r.status_code == 404
    r = admin_client.request(
        "DELETE", f"/api/ab/peer/{admin_guid}", content=json.dumps(["secret-peer"]), headers=alice
    )
    assert r.status_code == 404
    unknown = admin_client.post("/api/ab/peers", params={"ab": "no-such-guid"}, headers=alice)
    other = admin_client.post("/api/ab/peers", params={"ab": admin_guid}, headers=alice)
    assert unknown.json() == other.json()

    assert [p["id"] for p in _peers(admin_client, admin_headers, admin_guid)["data"]] == ["secret-peer"]


def test_shared_profiles_list_owned_and_shared_books_with_their_rule(admin_client):
    team = _new_book(admin_client, "Team")
    _new_book(admin_client, "Private")  # not shared with alice
    alice = _make_user(admin_client, "alice")
    _share(admin_client, team, "alice", 2)

    r = admin_client.post("/api/ab/shared/profiles", params={"current": 1, "pageSize": 100}, headers=alice)
    body = r.json()
    assert body["total"] == 1
    profile = body["data"][0]
    assert (profile["guid"], profile["name"], profile["owner"], profile["rule"]) == (team, "Team", "admin", 2)

    admin_profiles = admin_client.post("/api/ab/shared/profiles", headers=_login(admin_client)).json()
    assert {(p["name"], p["rule"]) for p in admin_profiles["data"]} == {("Team", 3), ("Private", 3)}


def test_rules_gate_writes_on_a_shared_book(admin_client):
    guid = _new_book(admin_client)
    admin_headers = _login(admin_client)
    _add(admin_client, admin_headers, guid, {"id": "111", "alias": "orig"})
    reader = _make_user(admin_client, "reader")
    writer = _make_user(admin_client, "writer")
    _share(admin_client, guid, "reader", 1)
    _share(admin_client, guid, "writer", 2)

    # Read-only: can read, every write is refused with a message the client shows.
    assert _peers(admin_client, reader, guid)["total"] == 1
    assert admin_client.post(f"/api/ab/tags/{guid}", headers=reader).status_code == 200
    denied = [
        _add(admin_client, reader, guid, {"id": "222"}),
        admin_client.put(f"/api/ab/peer/update/{guid}", json={"id": "111", "alias": "x"}, headers=reader),
        admin_client.request("DELETE", f"/api/ab/peer/{guid}", content=json.dumps(["111"]), headers=reader),
        admin_client.post(f"/api/ab/tag/add/{guid}", json={"name": "t", "color": 1}, headers=reader),
    ]
    assert [r.status_code for r in denied] == [403, 403, 403, 403]
    assert "read access" in denied[0].json()["error"]
    assert _peers(admin_client, admin_headers, guid)["data"][0]["alias"] == "orig"

    # Read/write: can change entries.
    assert _add(admin_client, writer, guid, {"id": "222", "alias": "by writer"}).status_code == 200
    admin_client.put(f"/api/ab/peer/update/{guid}", json={"id": "111", "alias": "edited"}, headers=writer)
    peers = {p["id"]: p["alias"] for p in _peers(admin_client, admin_headers, guid)["data"]}
    assert peers == {"111": "edited", "222": "by writer"}


def test_an_unshared_user_cannot_see_a_shared_book(admin_client):
    guid = _new_book(admin_client)
    stranger = _make_user(admin_client, "stranger")
    assert admin_client.post("/api/ab/peers", params={"ab": guid}, headers=stranger).status_code == 404
    assert admin_client.post("/api/ab/shared/profiles", headers=stranger).json()["total"] == 0


def test_removing_a_share_takes_access_away(admin_client):
    guid = _new_book(admin_client)
    alice = _make_user(admin_client, "alice")
    _share(admin_client, guid, "alice", 3)
    assert _peers(admin_client, alice, guid)["total"] == 0
    users = admin_client.get("/api/v1/address-books").json()
    shares = next(b for b in users if b["guid"] == guid)["shares"]
    r = admin_client.delete(f"/api/v1/address-books/{guid}/shares/{shares[0]['user_id']}")
    assert r.status_code == 200
    assert admin_client.post("/api/ab/peers", params={"ab": guid}, headers=alice).status_code == 404


def test_same_named_books_get_distinct_names_in_the_client(admin_client):
    """The client keys books by name, so two books called "Team" would replace
    one another."""
    alice = _make_user(admin_client, "alice")
    mine = _new_book(admin_client, "Team")
    _share(admin_client, mine, "alice", 1)
    alice_client_book = admin_client.post("/api/v1/auth/logout")  # switch the WebUI session to alice
    assert alice_client_book.status_code in (200, 204)
    admin_client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    admin_client.headers.update({"X-CSRF-Token": admin_client.cookies.get("rd_csrf")})
    own = admin_client.post("/api/v1/address-books", json={"name": "Team"}).json()["guid"]

    names = {
        p["guid"]: p["name"]
        for p in admin_client.post("/api/ab/shared/profiles", headers=alice).json()["data"]
    }
    assert set(names) == {mine, own}
    assert len(set(names.values())) == 2
    assert names[own] == "Team"
    assert names[mine] == "Team (admin)"
