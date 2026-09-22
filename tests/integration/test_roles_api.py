"""The role permission matrix and user groups (/api/v1/roles, /api/v1/user-groups):
CRUD is admin-only, and a role's grant actually changes what a non-admin can do
without ever widening which devices they can see."""


def _refresh_csrf(client):
    client.headers.update({"X-CSRF-Token": client.cookies.get("rd_csrf")})


def _login_as(client, username, password):
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login", json={"username": username, "password": password})
    _refresh_csrf(client)


def _create_user(admin_client, username, *, password="userpassword1", is_admin=False, role_id=None):
    payload = {"username": username, "password": password, "is_admin": is_admin}
    if role_id is not None:
        payload["role_id"] = role_id
    r = admin_client.post("/api/v1/users", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_roles_and_user_groups_are_admin_only(admin_client):
    _create_user(admin_client, "alice")
    _login_as(admin_client, "alice", "userpassword1")

    assert admin_client.get("/api/v1/roles").status_code == 403
    assert admin_client.post("/api/v1/roles", json={"name": "x", "permissions": {}}).status_code == 403
    assert admin_client.get("/api/v1/user-groups").status_code == 403


def test_role_crud(admin_client):
    r = admin_client.post(
        "/api/v1/roles",
        json={
            "name": "Support",
            "description": "Read access to devices and logs",
            "permissions": {"devices": "view", "logs": "view"},
            "requires_2fa": False,
        },
    )
    assert r.status_code == 201, r.text
    role = r.json()
    assert role["permissions"] == {"devices": "view", "logs": "view"}

    r = admin_client.get("/api/v1/roles")
    assert any(x["id"] == role["id"] for x in r.json())

    r = admin_client.patch(f"/api/v1/roles/{role['id']}", json={"permissions": {"devices": "manage"}})
    assert r.status_code == 200
    assert r.json()["permissions"] == {"devices": "manage"}

    r = admin_client.delete(f"/api/v1/roles/{role['id']}")
    assert r.status_code == 204
    assert admin_client.get("/api/v1/roles").status_code == 200
    assert role["id"] not in {x["id"] for x in admin_client.get("/api/v1/roles").json()}


def test_a_direct_role_grants_partial_access_without_making_someone_an_admin(admin_client):
    role = admin_client.post(
        "/api/v1/roles", json={"name": "User admin", "permissions": {"users": "manage"}}
    ).json()
    alice = _create_user(admin_client, "alice", role_id=role["id"])
    assert alice["is_admin"] is False
    assert alice["role_id"] == role["id"]

    _login_as(admin_client, "alice", "userpassword1")
    # Users:manage lets alice create and manage ordinary accounts...
    r = admin_client.post("/api/v1/users", json={"username": "bob", "password": "bobpassword1"})
    assert r.status_code == 201, r.text
    # ...but never is_admin or role_id, even though she otherwise has "manage".
    r = admin_client.post(
        "/api/v1/users", json={"username": "carol", "password": "carolpassword1", "is_admin": True}
    )
    assert r.status_code == 403
    r = admin_client.patch(f"/api/v1/users/{alice['id']}", json={"role_id": role["id"]})
    assert r.status_code == 403
    # A console area she was not granted stays forbidden.
    assert admin_client.get("/api/v1/strategies").status_code == 403


def test_a_view_only_role_cannot_write(admin_client):
    role = admin_client.post(
        "/api/v1/roles", json={"name": "Auditor", "permissions": {"users": "view"}}
    ).json()
    _create_user(admin_client, "alice", role_id=role["id"])
    _login_as(admin_client, "alice", "userpassword1")

    assert admin_client.get("/api/v1/users").status_code == 200
    r = admin_client.post("/api/v1/users", json={"username": "bob", "password": "bobpassword1"})
    assert r.status_code == 403


def test_a_role_via_a_user_group_grants_its_permissions(admin_client):
    role = admin_client.post(
        "/api/v1/roles", json={"name": "Log viewers", "permissions": {"logs": "manage"}}
    ).json()
    alice = _create_user(admin_client, "alice")
    r = admin_client.post(
        "/api/v1/user-groups",
        json={"name": "Support team", "role_id": role["id"], "member_ids": [alice["id"]]},
    )
    assert r.status_code == 201, r.text
    group = r.json()
    assert group["member_ids"] == [alice["id"]]

    _login_as(admin_client, "alice", "userpassword1")
    # logs:manage is required for the console audit trail (not just "view").
    assert admin_client.get("/api/v1/admin/audit-logs").status_code == 200


def test_deleting_a_role_removes_the_access_it_granted(admin_client):
    role = admin_client.post("/api/v1/roles", json={"name": "Temp", "permissions": {"users": "view"}}).json()
    _create_user(admin_client, "alice", role_id=role["id"])
    admin_client.delete(f"/api/v1/roles/{role['id']}")

    _login_as(admin_client, "alice", "userpassword1")
    assert admin_client.get("/api/v1/users").status_code == 403


def test_a_role_requiring_2fa_blocks_other_endpoints_until_it_is_enabled(admin_client):
    role = admin_client.post(
        "/api/v1/roles", json={"name": "Sensitive", "permissions": {"logs": "view"}, "requires_2fa": True}
    ).json()
    _create_user(admin_client, "alice", role_id=role["id"])
    _login_as(admin_client, "alice", "userpassword1")

    r = admin_client.get("/api/v1/connection-logs")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "TWO_FACTOR_SETUP_REQUIRED"
    # The 2FA setup endpoints themselves, and /me, stay reachable.
    assert admin_client.get("/api/v1/auth/2fa").status_code == 200
    assert admin_client.get("/api/v1/auth/me").status_code == 200
