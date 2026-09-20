"""RustDesk client sysinfo/device-registration protocol (POST /api/sysinfo).

NOT YET VERIFIED against a real RustDesk desktop client - see
docs/rustdesk-compatibility.md.
"""


def test_sysinfo_registers_a_new_device(client):
    r = client.post(
        "/api/sysinfo",
        json={
            "id": "abc-123",
            "uuid": "uuid-1",
            "hostname": "DESKTOP-1",
            "os": "Windows 11",
            "username": "alice",
            "version": "1.3.0",
            "cpu": "Intel i7",
            "memory": "16GB",
        },
    )
    assert r.status_code == 200
    assert r.text == "SYSINFO_UPDATED"


def test_sysinfo_acknowledgement_is_the_exact_plain_text_the_client_checks(client):
    """The 1.4.9 client compares the body with `SYSINFO_UPDATED` (`hbbs_http/sync.rs`); JSON, quotes or a
    trailing newline all make it treat the upload as failed and repeat it every ~2 minutes."""
    r = client.post("/api/sysinfo", json={"id": "abc-124", "hostname": "h"})
    assert r.content == b"SYSINFO_UPDATED"
    assert r.headers["content-type"].startswith("text/plain")


def test_sysinfo_persists_cpu_and_memory(admin_client):
    admin_client.post(
        "/api/sysinfo",
        json={
            "id": "dev-specs",
            "hostname": "h",
            "os": "windows",
            "cpu": "Intel i7",
            "memory": "16GB",
        },
    )
    devices = admin_client.get("/api/v1/devices").json()["items"]
    device = next(d for d in devices if d["rustdesk_id"] == "dev-specs")
    assert device["cpu"] == "Intel i7"
    assert device["memory"] == "16GB"


def test_sysinfo_missing_id_returns_error(client):
    r = client.post("/api/sysinfo", json={"hostname": "x"})
    assert r.status_code == 200
    assert "error" in r.json()


def test_sysinfo_is_idempotent_for_same_device_id(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}

    admin_client.post(
        "/api/sysinfo", json={"id": "dev-1", "hostname": "host-a", "os": "windows"}, headers=headers
    )
    admin_client.post(
        "/api/sysinfo", json={"id": "dev-1", "hostname": "host-a-renamed", "os": "windows"}, headers=headers
    )

    body = admin_client.get("/api/v1/devices").json()
    assert body["total"] == 1
    assert body["items"][0]["hostname"] == "host-a-renamed"


def test_sysinfo_with_auth_token_claims_ownership(admin_client):
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    admin_client.post(
        "/api/sysinfo",
        json={"id": "dev-owned", "hostname": "h", "os": "windows"},
        headers={"Authorization": f"Bearer {token}"},
    )
    device = admin_client.get("/api/v1/devices").json()["items"][0]
    admin_id = admin_client.get("/api/v1/auth/me").json()["id"]
    assert device["owner_id"] == admin_id


def test_sysinfo_splits_composite_os_string_into_platform_and_os_version(admin_client):
    """Confirmed against a real RustDesk 1.4.9 Windows client (see
    docs/rustdesk-compatibility.md): `os` is sent as
    "<platform> / <full OS description>"."""
    r = admin_client.post(
        "/api/sysinfo",
        json={
            "id": "dev-real",
            "hostname": "desktop-example",
            "os": "windows / Windows 11 IoT Enterprise LTSC 2024 - 11 (26300)",
            "version": "1.4.9",
        },
    )
    assert r.status_code == 200

    devices = admin_client.get("/api/v1/devices").json()["items"]
    device = next(d for d in devices if d["rustdesk_id"] == "dev-real")
    assert device["platform"] == "windows"
    assert device["os_version"] == "Windows 11 IoT Enterprise LTSC 2024 - 11 (26300)"


def test_sysinfo_os_without_separator_falls_back_to_platform_only(admin_client):
    r = admin_client.post("/api/sysinfo", json={"id": "dev-legacy", "os": "Windows 11"})
    assert r.status_code == 200

    devices = admin_client.get("/api/v1/devices").json()["items"]
    device = next(d for d in devices if d["rustdesk_id"] == "dev-legacy")
    assert device["platform"] == "Windows 11"
    assert device["os_version"] is None
