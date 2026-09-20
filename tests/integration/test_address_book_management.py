import json


def test_empty_when_nothing_synced(admin_client):
    r = admin_client.get("/api/v1/address-book")
    assert r.status_code == 200
    assert r.json() == []


def test_reflects_entries_pushed_via_rustdesk_protocol(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    data = {
        "tags": [],
        "peers": [{"id": "12345", "alias": "my-laptop", "hostname": "laptop", "tags": []}],
        "tag_colors": "{}",
    }
    admin_client.post(
        "/api/ab", json={"data": json.dumps(data)}, headers={"Authorization": f"Bearer {token}"}
    )

    r = admin_client.get("/api/v1/address-book")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["rustdesk_id"] == "12345"
    assert body[0]["alias"] == "my-laptop"


def test_password_hash_is_never_exposed_only_a_boolean_flag(admin_client):
    """Given an entry the client synced with a non-empty `hash` (its own
    connection-password field), when it's read back through the management
    API, then only a has_password boolean is exposed - never the raw value
    (CLAUDE.md section 18), matching the reference project's own
    has_rhash-not-rhash display."""
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    data = {
        "tags": [],
        "peers": [{"id": "with-pw", "alias": "a", "tags": [], "hash": "some-opaque-client-value"}],
        "tag_colors": "{}",
    }
    admin_client.post(
        "/api/ab", json={"data": json.dumps(data)}, headers={"Authorization": f"Bearer {token}"}
    )

    r = admin_client.get("/api/v1/address-book")
    body = r.json()
    entry = next(e for e in body if e["rustdesk_id"] == "with-pw")
    assert entry["has_password"] is True
    assert "hash" not in entry
    assert "some-opaque-client-value" not in r.text


def test_manually_created_entry_has_no_password(admin_client):
    r = admin_client.post("/api/v1/address-book", json={"rustdesk_id": "no-pw", "alias": "a"})
    assert r.json()["has_password"] is False


def test_requires_authentication(client):
    r = client.get("/api/v1/address-book")
    assert r.status_code == 401


def test_scoped_to_own_entries_only(admin_client):
    token_admin = admin_client.post(
        "/api/login", json={"username": "admin", "password": "adminpass123"}
    ).json()["access_token"]
    data = {"tags": [], "peers": [{"id": "admin-only", "alias": "a", "tags": []}], "tag_colors": "{}"}
    admin_client.post(
        "/api/ab", json={"data": json.dumps(data)}, headers={"Authorization": f"Bearer {token_admin}"}
    )

    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    admin_client.post("/api/v1/auth/logout")
    admin_client.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepassword1"})
    csrf = admin_client.cookies.get("rd_csrf")
    admin_client.headers.update({"X-CSRF-Token": csrf})

    r = admin_client.get("/api/v1/address-book")
    assert r.json() == []


def test_create_entry_via_webui(admin_client):
    r = admin_client.post(
        "/api/v1/address-book",
        json={"rustdesk_id": "999888777", "alias": "manual-entry", "tags": ["prod"]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["rustdesk_id"] == "999888777"
    assert body["alias"] == "manual-entry"
    assert [t["name"] for t in body["tags"]] == ["prod"]

    r = admin_client.get("/api/v1/address-book")
    assert any(e["rustdesk_id"] == "999888777" for e in r.json())


def test_create_entry_rejects_duplicate_rustdesk_id(admin_client):
    admin_client.post("/api/v1/address-book", json={"rustdesk_id": "111", "alias": "a"})
    r = admin_client.post("/api/v1/address-book", json={"rustdesk_id": "111", "alias": "b"})
    assert r.status_code == 409


def test_update_entry_via_webui(admin_client):
    entry = admin_client.post("/api/v1/address-book", json={"rustdesk_id": "222", "alias": "old"}).json()
    r = admin_client.patch(
        f"/api/v1/address-book/{entry['id']}",
        json={"alias": "new", "hostname": "h", "platform": "windows", "tags": ["home"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["alias"] == "new"
    assert body["hostname"] == "h"
    assert [t["name"] for t in body["tags"]] == ["home"]


def test_delete_entry_via_webui(admin_client):
    entry = admin_client.post("/api/v1/address-book", json={"rustdesk_id": "333", "alias": "a"}).json()
    r = admin_client.delete(f"/api/v1/address-book/{entry['id']}")
    assert r.status_code == 204

    r = admin_client.get("/api/v1/address-book")
    assert r.json() == []


def test_cannot_update_or_delete_another_users_entry(admin_client):
    """Given an entry owned by admin, when another user tries to edit or
    delete it by id (IDOR probe), then the server returns 404 for both,
    same as the RustDesk-protocol data (CLAUDE.md section 65)."""
    entry = admin_client.post("/api/v1/address-book", json={"rustdesk_id": "444", "alias": "a"}).json()

    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    admin_client.post("/api/v1/auth/logout")
    alice_client = admin_client
    alice_client.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepassword1"})
    csrf = alice_client.cookies.get("rd_csrf")
    alice_client.headers.update({"X-CSRF-Token": csrf})

    r = alice_client.patch(f"/api/v1/address-book/{entry['id']}", json={"alias": "hijacked"})
    assert r.status_code == 404

    r = alice_client.delete(f"/api/v1/address-book/{entry['id']}")
    assert r.status_code == 404


def _push_entry(client, token, rustdesk_id):
    data = {
        "tags": [],
        "peers": [{"id": rustdesk_id, "alias": "a", "hostname": "h", "tags": []}],
        "tag_colors": "{}",
    }
    r = client.post("/api/ab", json={"data": json.dumps(data)}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def _login_as(client, username, password):
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login", json={"username": username, "password": password})
    client.headers.update({"X-CSRF-Token": client.cookies.get("rd_csrf")})


def _admin_token(client):
    return client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]


def test_entry_links_to_the_matching_device(admin_client):
    """Given an address book entry whose RustDesk ID is a registered device
    When the owner lists their address book
    Then the entry carries that device's id, and an unmatched one does not."""
    token = _admin_token(admin_client)
    admin_client.post(
        "/api/sysinfo",
        json={"id": "dev-1", "hostname": "h", "os": "windows"},
        headers={"Authorization": f"Bearer {token}"},
    )
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]
    data = {
        "tags": [],
        "peers": [
            {"id": "dev-1", "alias": "a", "tags": []},
            {"id": "no-such-device", "alias": "b", "tags": []},
        ],
        "tag_colors": "{}",
    }
    admin_client.post(
        "/api/ab", json={"data": json.dumps(data)}, headers={"Authorization": f"Bearer {token}"}
    )

    by_id = {e["rustdesk_id"]: e for e in admin_client.get("/api/v1/address-book").json()}
    assert by_id["dev-1"]["device_id"] == device_id
    assert by_id["no-such-device"]["device_id"] is None


def test_entry_never_links_to_a_device_the_user_cannot_view(admin_client):
    """IDOR: alice's address book lists an ID that is admin's private device.
    The device exists, but alice must not be told so."""
    token = _admin_token(admin_client)
    admin_client.post(
        "/api/sysinfo",
        json={"id": "dev-1", "hostname": "h", "os": "windows"},
        headers={"Authorization": f"Bearer {token}"},
    )
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    alice_token = admin_client.post(
        "/api/login", json={"username": "alice", "password": "alicepassword1"}
    ).json()["access_token"]
    _push_entry(admin_client, alice_token, "dev-1")
    _login_as(admin_client, "alice", "alicepassword1")

    entry = admin_client.get("/api/v1/address-book").json()[0]
    assert entry["rustdesk_id"] == "dev-1"
    assert entry["device_id"] is None


def test_entry_links_once_the_device_is_shared_with_the_user(admin_client):
    token = _admin_token(admin_client)
    admin_client.post(
        "/api/sysinfo",
        json={"id": "dev-1", "hostname": "h", "os": "windows"},
        headers={"Authorization": f"Bearer {token}"},
    )
    device_id = admin_client.get("/api/v1/devices").json()["items"][0]["id"]
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    admin_client.post(f"/api/v1/devices/{device_id}/shares", json={"username": "alice", "permission": "view"})
    alice_token = admin_client.post(
        "/api/login", json={"username": "alice", "password": "alicepassword1"}
    ).json()["access_token"]
    _push_entry(admin_client, alice_token, "dev-1")
    _login_as(admin_client, "alice", "alicepassword1")

    assert admin_client.get("/api/v1/address-book").json()[0]["device_id"] == device_id
