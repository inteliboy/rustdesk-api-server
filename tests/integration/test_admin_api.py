def test_dashboard_requires_admin(admin_client):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    admin_client.post("/api/v1/auth/logout")
    admin_client.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepassword1"})

    r = admin_client.get("/api/v1/admin/dashboard")
    assert r.status_code == 403


def test_dashboard_counts_devices_and_users(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    admin_client.post(
        "/api/sysinfo",
        json={"id": "dev-1", "hostname": "h", "os": "windows"},
        headers={"Authorization": f"Bearer {token}"},
    )

    r = admin_client.get("/api/v1/admin/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert body["total_devices"] == 1
    assert body["online_devices"] == 1
    assert body["offline_devices"] == 0
    assert body["total_users"] == 1


def test_audit_log_records_login_and_user_creation(admin_client):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    r = admin_client.get("/api/v1/admin/audit-logs")
    assert r.status_code == 200
    actions = {item["action"] for item in r.json()["items"]}
    assert "login" in actions
    assert "user_created" in actions


def test_audit_log_never_contains_password_field(admin_client):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "super-secret-password", "is_admin": False}
    )
    r = admin_client.get("/api/v1/admin/audit-logs")
    raw = r.text
    assert "super-secret-password" not in raw


def test_audit_log_includes_actor_username_and_detail(admin_client):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    r = admin_client.get("/api/v1/admin/audit-logs")
    items = r.json()["items"]
    created = next(i for i in items if i["action"] == "user_created")
    assert created["actor_username"] == "admin"
    assert created["detail"] == {"username": "alice"}


def test_audit_log_detail_survives_target_deletion(admin_client):
    """Given a tag that gets deleted, when its audit entry is read back,
    then the tag's name is still available in `detail` even though the tag
    row itself is gone (name was captured before deletion, not looked up
    from the now-missing row)."""
    tag = admin_client.post("/api/v1/tags", json={"name": "ephemeral"}).json()
    admin_client.delete(f"/api/v1/tags/{tag['id']}")

    r = admin_client.get("/api/v1/admin/audit-logs")
    deleted = next(i for i in r.json()["items"] if i["action"] == "tag_deleted")
    assert deleted["detail"] == {"name": "ephemeral"}


def test_audit_log_filters_by_actor_id(admin_client):
    admin_id = admin_client.get("/api/v1/auth/me").json()["id"]
    r = admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    alice_id = r.json()["id"]

    r = admin_client.get(f"/api/v1/admin/audit-logs?actor_id={admin_id}")
    assert r.status_code == 200
    assert all(item["actor_id"] == admin_id for item in r.json()["items"])
    actions = {item["action"] for item in r.json()["items"]}
    assert "user_created" in actions

    r = admin_client.get(f"/api/v1/admin/audit-logs?actor_id={alice_id}")
    assert r.json()["items"] == []


CHROME_WIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


def test_first_run_setup_audit_entry_names_the_administrator(client):
    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    client.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"})
    client.headers.update({"X-CSRF-Token": client.cookies.get("rd_csrf")})
    items = client.get("/api/v1/admin/audit-logs").json()["items"]
    setup = next(i for i in items if i["action"] == "user_created")
    assert setup["detail"] == {"via": "setup", "username": "admin"}


def test_login_audit_entries_record_ip_and_browser(client):
    """Given a WebUI login and a failed one from a browser
    When an admin reads the audit log
    Then both entries say which browser/OS and IP they came from."""
    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    headers = {"User-Agent": CHROME_WIN_UA}
    client.post("/api/v1/auth/login", json={"username": "nobody", "password": "wrongpass1"}, headers=headers)
    client.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"}, headers=headers)
    client.headers.update({"X-CSRF-Token": client.cookies.get("rd_csrf")})

    logins = [i for i in client.get("/api/v1/admin/audit-logs").json()["items"] if i["action"] == "login"]
    ok = next(i for i in logins if i["result"] == "success")
    bad = next(i for i in logins if i["result"] == "failure")
    for entry in (ok, bad):
        assert entry["ip_address"]
        assert entry["detail"]["browser"] == "Chrome 130"
        assert entry["detail"]["os"] == "Windows"
        assert entry["detail"]["via"] == "webui"
    assert ok["actor_username"] == "admin"
    assert bad["detail"]["username"] == "nobody"


def test_rustdesk_client_login_audit_records_its_agent(client):
    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    client.post(
        "/api/login",
        json={"username": "admin", "password": "adminpass123"},
        headers={"User-Agent": "reqwest/0.11.24"},
    )
    client.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"})
    client.headers.update({"X-CSRF-Token": client.cookies.get("rd_csrf")})

    logins = [i for i in client.get("/api/v1/admin/audit-logs").json()["items"] if i["action"] == "login"]
    via = {i["detail"]["via"]: i["detail"] for i in logins}
    assert via["rustdesk_client"]["browser"] == "RustDesk client"
    assert "password" not in str(via)


def test_rustdesk_client_login_audit_records_the_version_the_device_reported(client):
    """The client's login carries no version and no User-Agent, so the version is
    the one the same device (id + uuid) last reported in sysinfo."""
    client.post("/api/v1/auth/setup", json={"username": "admin", "password": "adminpass123"})
    client.post(
        "/api/sysinfo",
        json={"id": "111222333", "uuid": "u1", "hostname": "h", "os": "windows", "version": "1.4.9"},
    )
    body = {"username": "admin", "password": "adminpass123", "id": "111222333", "uuid": "u1"}
    client.post("/api/login", json=body)
    client.post("/api/login", json={**body, "password": "wrongpass1"})
    # A different uuid must not borrow that device's version; nor may an unknown id.
    client.post("/api/login", json={**body, "uuid": "someone-else"})
    client.post("/api/login", json={**body, "id": "999"})
    client.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"})
    client.headers.update({"X-CSRF-Token": client.cookies.get("rd_csrf")})

    logins = [
        i["detail"]
        for i in client.get("/api/v1/admin/audit-logs").json()["items"]
        if i["action"] == "login" and i["detail"].get("via") == "rustdesk_client"
    ]
    assert len(logins) == 4
    assert sorted(bool(d.get("client_version")) for d in logins) == [False, False, True, True]
    assert {d["client_version"] for d in logins if "client_version" in d} == {"1.4.9"}
