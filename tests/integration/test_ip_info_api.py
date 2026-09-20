"""/api/v1/ip-info - never touches the network: the service is given a fake fetcher."""

import pytest

from rustdesk_api.services.ip_info import IpInfoService, IpLookupError

RDAP = {
    "name": "GOGL",
    "country": "US",
    "port43": "whois.arin.net",
    "cidr0_cidrs": [{"v4prefix": "8.8.8.0", "length": 24}],
    "entities": [{"roles": ["registrant"], "vcardArray": ["vcard", [["fn", {}, "text", "Google LLC"]]]}],
}


@pytest.fixture()
def fetched(app):
    """Installs a fake registry on the app and returns the list of queried IPs."""
    calls: list[str] = []

    def fake(ip, timeout):
        calls.append(ip)
        return RDAP

    app.state.ip_info_service = IpInfoService(enabled=True, timeout=1.0, max_per_minute=50, fetch=fake)
    return calls


def test_public_ip_returns_whois_style_details(admin_client, fetched):
    r = admin_client.get("/api/v1/ip-info", params={"ip": "8.8.8.8"})
    assert r.status_code == 200
    assert r.json() == {
        "ip": "8.8.8.8",
        "status": "ok",
        "network": "GOGL",
        "organization": "Google LLC",
        "country": "US",
        "cidr": "8.8.8.0/24",
        "registry": "whois.arin.net",
    }
    assert fetched == ["8.8.8.8"]


@pytest.mark.parametrize("ip", ["127.0.0.1", "192.168.1.20", "10.0.0.5", "::1", "fd00::1", "169.254.1.1"])
def test_local_addresses_are_reported_local_and_never_looked_up(admin_client, fetched, ip):
    r = admin_client.get("/api/v1/ip-info", params={"ip": ip})
    assert r.status_code == 200
    assert r.json()["status"] == "local"
    assert fetched == []


@pytest.mark.parametrize("ip", ["not-an-ip", "8.8.8.8/24", "http://8.8.8.8", "1.2.3.4 ; ls"])
def test_invalid_input_is_rejected_without_a_lookup(admin_client, fetched, ip):
    r = admin_client.get("/api/v1/ip-info", params={"ip": ip})
    assert r.status_code == 422
    assert fetched == []


def test_requires_authentication(client, fetched):
    assert client.get("/api/v1/ip-info", params={"ip": "8.8.8.8"}).status_code == 401
    assert fetched == []


def test_ordinary_users_may_look_up_public_ips(admin_client, fetched):
    admin_client.post(
        "/api/v1/users", json={"username": "alice", "password": "alicepassword1", "is_admin": False}
    )
    admin_client.post("/api/v1/auth/logout")
    admin_client.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepassword1"})
    assert admin_client.get("/api/v1/ip-info", params={"ip": "8.8.8.8"}).json()["status"] == "ok"


def test_registry_failure_degrades_to_unavailable_not_an_error(admin_client, app):
    def down(ip, timeout):
        raise IpLookupError("registry down")

    app.state.ip_info_service = IpInfoService(enabled=True, timeout=1.0, max_per_minute=50, fetch=down)
    r = admin_client.get("/api/v1/ip-info", params={"ip": "8.8.8.8"})
    assert r.status_code == 200
    assert r.json()["status"] == "unavailable"
    assert r.json()["organization"] is None


def test_can_be_disabled_by_configuration(admin_client, app, monkeypatch):
    called = []
    app.state.ip_info_service = IpInfoService(
        enabled=False, timeout=1.0, max_per_minute=50, fetch=lambda ip, t: called.append(ip) or RDAP
    )
    r = admin_client.get("/api/v1/ip-info", params={"ip": "8.8.8.8"})
    assert r.json()["status"] == "disabled"
    assert called == []


def test_service_is_built_from_settings_when_not_injected(admin_client, app, monkeypatch):
    monkeypatch.setenv("IP_LOOKUP_ENABLED", "false")
    from rustdesk_api.config import clear_settings_cache

    clear_settings_cache()
    app.state.ip_info_service = None
    assert admin_client.get("/api/v1/ip-info", params={"ip": "8.8.8.8"}).json()["status"] == "disabled"
