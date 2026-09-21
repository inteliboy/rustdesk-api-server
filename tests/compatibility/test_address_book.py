"""RustDesk client address-book / peer-management protocol.

STATUS: partially verified - see docs/rustdesk-compatibility.md. Confirmed
via a combination of live capture against a real RustDesk 1.4.9 client and
reading the reference project's source (api/views_api.py::ab):

- `GET /api/ab` and `POST /api/ab` share one path, matching the reference
  project's single `ab()` view dispatching on method.
- `data` must be a JSON-encoded *string* decoding to
  {"tags": [...], "peers": [...], "tag_colors": "<json string>"} - a bare
  array previously caused a real client to raise
  "type 'String' is not a subtype of type 'int' of 'index'".
- Address book entries are independent, client-managed records (a user can
  add an entry for a peer id this server has never seen via /api/sysinfo) -
  stored in AddressBookEntry, not derived from Device.

`login-options`, `device-group/accessible`, `/api/users`, and `/api/peers`
are not implemented in the reference project (`peers` is a stub). Their shapes
here come from the client's own parser (`GroupModel` in
flutter/lib/models/group_model.dart, `PeerPayload` in
flutter/lib/common/hbbs/hbbs.dart, 1.4.9): `data` is a real JSON array, unlike
`/api/ab`. Read from source, not yet seen working in a live client.
"""

import json


def _login_get_token(client, username="admin", password="adminpass123"):
    return client.post("/api/login", json={"username": username, "password": password}).json()["access_token"]


def _push_ab(client, token, peers, tag_colors=None):
    data = {"tags": [], "peers": peers, "tag_colors": json.dumps(tag_colors or {})}
    return client.post(
        "/api/ab", json={"data": json.dumps(data)}, headers={"Authorization": f"Bearer {token}"}
    )


def test_login_options_returns_an_empty_array(client):
    """The client's only reader (`UserModel.queryOidcLoginOptions`) does
    `for (final item in jsonDecode(resp.body))`, so a JSON object breaks it:
    1.4.9 swallows that, `master` shows a network error with a Retry button.
    An earlier version answered `{}` after a real 1.4.9 client raised "type
    'String' is not a subtype of type 'int' of 'index'"; that message is a
    string key used on a list, which nothing in this code path does, so it was
    most likely another endpoint's response."""
    r = client.get("/api/login-options")
    assert r.status_code == 200
    assert r.json() == []


def test_ab_get_returns_confirmed_envelope_shape_when_empty(client):
    r = client.get("/api/ab")
    assert r.status_code == 200
    body = r.json()
    assert "updated_at" in body
    assert isinstance(body["data"], str)
    data = json.loads(body["data"])
    assert data == {"tags": [], "peers": [], "tag_colors": "{}"}


def test_device_group_accessible_without_auth_returns_empty(client):
    r = client.get("/api/device-group/accessible", params={"current": 1, "pageSize": 100})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["data"] == []


def test_device_group_accessible_lists_owned_groups(admin_client):
    admin_client.post("/api/v1/groups", json={"name": "Servers"})
    token = _login_get_token(admin_client)

    r = admin_client.get(
        "/api/device-group/accessible",
        params={"current": 1, "pageSize": 100},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["data"] == [{"name": "Servers"}]


def test_ab_personal_without_auth_returns_401_error(client):
    r = client.post("/api/ab/personal")
    assert r.status_code == 401
    assert "error" in r.json()


def test_ab_post_without_auth_returns_error(client):
    r = client.post("/api/ab", json={"data": "{}"})
    assert r.status_code == 200
    assert "error" in r.json()


def test_ab_post_persists_a_manually_added_entry(admin_client):
    """Confirmed (2026-09-18, live capture): a real client pushed exactly
    this shape after a user manually added a peer not otherwise known to
    this server (rustdesk id the server never saw a sysinfo/heartbeat
    call for)."""
    token = _login_get_token(admin_client)
    peers = [
        {
            "id": "75646747685",
            "username": "",
            "hostname": "",
            "platform": "",
            "alias": "test",
            "tags": [],
            "hash": "",
        }
    ]
    r = _push_ab(admin_client, token, peers)
    assert r.status_code == 200
    # Must NOT contain an "error" key on success - the real client's
    # LegacyAb.pushAb() does `json.containsKey('error')` and throws
    # whatever value is there, even an empty string (see api/address_book.py).
    assert "error" not in r.json()

    r = admin_client.get("/api/ab", headers={"Authorization": f"Bearer {token}"})
    data = json.loads(r.json()["data"])
    peer = next(p for p in data["peers"] if p["id"] == "75646747685")
    assert peer["alias"] == "test"


def test_ab_post_is_a_full_replace(admin_client):
    """The client pushes its entire local address book on every save
    (confirmed 2026-09-18) - a second push without an entry from the first
    removes it server-side, rather than merging."""
    token = _login_get_token(admin_client)
    _push_ab(admin_client, token, [{"id": "dev-a", "alias": "a", "tags": []}])
    _push_ab(admin_client, token, [{"id": "dev-b", "alias": "b", "tags": []}])

    r = admin_client.get("/api/ab", headers={"Authorization": f"Bearer {token}"})
    data = json.loads(r.json()["data"])
    ids = {p["id"] for p in data["peers"]}
    assert ids == {"dev-b"}


def test_ab_post_updates_existing_entry_in_place(admin_client):
    token = _login_get_token(admin_client)
    _push_ab(admin_client, token, [{"id": "dev-a", "alias": "first", "tags": []}])
    _push_ab(admin_client, token, [{"id": "dev-a", "alias": "renamed", "tags": []}])

    r = admin_client.get("/api/ab", headers={"Authorization": f"Bearer {token}"})
    data = json.loads(r.json()["data"])
    assert len(data["peers"]) == 1
    assert data["peers"][0]["alias"] == "renamed"


def test_ab_post_creates_tags_with_colors(admin_client):
    token = _login_get_token(admin_client)
    peers = [{"id": "dev-a", "alias": "a", "tags": ["prod"]}]
    # The client's colors are ARGB integers (`Color.value`).
    r = _push_ab(admin_client, token, peers, tag_colors={"prod": 0xFFFF0000})
    assert r.status_code == 200

    r = admin_client.get("/api/ab", headers={"Authorization": f"Bearer {token}"})
    data = json.loads(r.json()["data"])
    assert "prod" in data["tags"]
    tag_colors = json.loads(data["tag_colors"])
    assert tag_colors["prod"] == 0xFFFF0000

    # Address-book tags are per-book: they do not leak into the global device
    # tag vocabulary.
    r = admin_client.get("/api/v1/tags")
    assert not any(t["name"] == "prod" for t in r.json())


def test_ab_entries_are_scoped_per_user(admin_client):
    token_admin = _login_get_token(admin_client)
    _push_ab(admin_client, token_admin, [{"id": "dev-admin-only", "alias": "a", "tags": []}])

    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    token_alice = _login_get_token(admin_client, "alice", "alicepassword1")

    r = admin_client.get("/api/ab", headers={"Authorization": f"Bearer {token_alice}"})
    data = json.loads(r.json()["data"])
    assert data["peers"] == []


def test_ab_get_keeps_tags_that_no_peer_uses(admin_client):
    token = _login_get_token(admin_client)
    inner = {
        "tags": ["spare"],
        "peers": [{"id": "dev-a", "tags": []}],
        "tag_colors": json.dumps({"spare": 1}),
    }
    admin_client.post(
        "/api/ab",
        json={"data": json.dumps(inner)},
        headers={"Authorization": f"Bearer {token}"},
    )
    r = admin_client.get("/api/ab", headers={"Authorization": f"Bearer {token}"})
    assert json.loads(r.json()["data"])["tags"] == ["spare"]


def test_users_listing_without_auth_returns_empty(client):
    r = client.get("/api/users", params={"current": 1, "pageSize": 100, "status": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["data"] == []


def test_accessible_lookups_return_a_json_array_not_a_string(admin_client):
    """The client does `if (data is List)` (group_model.dart) and ignores
    anything else - a JSON-encoded string left the panel empty."""
    token = _login_get_token(admin_client)
    headers = {"Authorization": f"Bearer {token}"}
    params = {"current": 1, "pageSize": 100, "accessible": "", "status": 1}
    for path in ("/api/users", "/api/peers", "/api/device-group/accessible"):
        body = admin_client.get(path, params=params, headers=headers).json()
        assert isinstance(body["total"], int), path
        assert isinstance(body["data"], list), path


def test_users_listing_is_the_caller_alone_by_default(admin_client):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    token = _login_get_token(admin_client, "alice", "alicepassword1")

    r = admin_client.get(
        "/api/users",
        params={"current": 1, "pageSize": 100, "status": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert [u["name"] for u in data] == ["alice"]
    assert data[0]["status"] == 1
    assert data[0]["is_admin"] is False


def test_users_listing_adds_owners_of_devices_shared_with_the_caller(admin_client):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    admin_client.post(
        "/api/login",
        json={"username": "admin", "password": "adminpass123", "id": "777888999"},
    )
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]
    admin_client.post(f"/api/v1/devices/{device_id}/shares", json={"username": "alice", "permission": "view"})
    token = _login_get_token(admin_client, "alice", "alicepassword1")

    r = admin_client.get(
        "/api/users",
        params={"current": 1, "pageSize": 100, "status": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert [u["name"] for u in r.json()["data"]] == ["alice", "admin"]


def test_peers_without_auth_returns_empty(client):
    r = client.get("/api/peers", params={"current": 1, "pageSize": 100, "status": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["data"] == []


def test_peers_lists_devices_owned_by_caller_in_the_shape_the_client_reads(admin_client):
    """A real client's login body includes its own `id`, which
    /api/login opportunistically registers and claims (see
    api/auth.py) - reused here to get a Device row without needing a
    separate heartbeat/sysinfo call. Field names are those of `PeerPayload`
    in the client source."""
    admin_client.post(
        "/api/login",
        json={"username": "admin", "password": "adminpass123", "id": "111222333"},
    )
    token = _login_get_token(admin_client)
    admin_client.post(
        "/api/sysinfo",
        json={"id": "111222333", "hostname": "desk-1", "username": "bob", "os": "Windows 11"},
        headers={"Authorization": f"Bearer {token}"},
    )
    group = admin_client.post("/api/v1/groups", json={"name": "Servers"}).json()
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]
    admin_client.patch(f"/api/v1/devices/{device_id}", json={"group_id": group["id"], "note": "front desk"})

    r = admin_client.get(
        "/api/peers",
        params={"current": 1, "pageSize": 100, "accessible": "", "status": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    (peer,) = body["data"]
    assert peer["id"] == "111222333"
    assert peer["user_name"] == "admin"
    assert peer["device_group_name"] == "Servers"
    assert peer["note"] == "front desk"
    assert peer["status"] == 1
    assert peer["info"] == {"username": "bob", "os": "windows", "device_name": "desk-1"}


def test_peers_are_scoped_per_user(admin_client):
    admin_client.post(
        "/api/login",
        json={"username": "admin", "password": "adminpass123", "id": "444555666"},
    )

    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    token_alice = _login_get_token(admin_client, "alice", "alicepassword1")

    r = admin_client.get(
        "/api/peers",
        params={"current": 1, "pageSize": 100, "status": 1},
        headers={"Authorization": f"Bearer {token_alice}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["data"] == []
