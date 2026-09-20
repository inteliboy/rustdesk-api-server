import urllib.error

import pytest

from rustdesk_api.services import ip_info
from rustdesk_api.services.ip_info import IpInfoService, IpLookupError, parse_ip, parse_rdap

# Trimmed from real registry answers (rdap.org -> ARIN / RIPE).
ARIN = {
    "name": "GOGL",
    "startAddress": "8.8.8.0",
    "endAddress": "8.8.8.255",
    "port43": "whois.arin.net",
    "cidr0_cidrs": [{"v4prefix": "8.8.8.0", "length": 24}],
    "entities": [
        {
            "handle": "GOGL",
            "roles": ["registrant"],
            "vcardArray": ["vcard", [["fn", {}, "text", "Google LLC"]]],
        }
    ],
}
RIPE = {
    "name": "RIPE-NCC",
    "country": "nl",
    "port43": "whois.ripe.net",
    "cidr0_cidrs": [{"v4prefix": "193.0.0.0", "length": 21}],
    "entities": [
        {"roles": ["technical"], "vcardArray": ["vcard", [["fn", {}, "text", "Ops"]]]},
        {"roles": ["registrant"], "vcardArray": ["vcard", [["fn", {}, "text", "RIPE NCC"]]]},
    ],
}


@pytest.mark.parametrize(
    ("value", "public"),
    [
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("::ffff:8.8.4.4", True),
        ("10.0.0.1", False),
        ("192.168.1.10", False),
        ("172.16.5.5", False),
        ("127.0.0.1", False),
        ("169.254.10.10", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fe80::1%eth0", False),
        ("fd00::1", False),
        ("::ffff:10.0.0.1", False),
    ],
)
def test_only_globally_routable_addresses_count_as_public(value, public):
    addr = parse_ip(value)
    assert addr is not None
    assert ip_info.is_public_ip(addr) is public


@pytest.mark.parametrize(
    "value", ["", "not-an-ip", "8.8.8", "8.8.8.8/24", "1.2.3.4; rm -rf /", "http://8.8.8.8"]
)
def test_non_ip_input_is_rejected(value):
    assert parse_ip(value) is None


def test_parse_rdap_extracts_the_basic_whois_fields():
    info = parse_rdap(ARIN)
    assert (info.network, info.organization, info.cidr, info.registry) == (
        "GOGL",
        "Google LLC",
        "8.8.8.0/24",
        "whois.arin.net",
    )
    assert info.country is None  # ARIN does not report one


def test_parse_rdap_uses_registrant_not_other_roles_and_upcases_country():
    info = parse_rdap(RIPE)
    assert info.organization == "RIPE NCC"
    assert info.country == "NL"


def test_parse_rdap_falls_back_to_range_and_remarks():
    info = parse_rdap(
        {
            "startAddress": "1.1.1.0",
            "endAddress": "1.1.1.255",
            "remarks": [{"description": ["APNIC Labs", "more"]}],
        }
    )
    assert info.cidr == "1.1.1.0 - 1.1.1.255"
    assert info.organization == "APNIC Labs"


def test_parse_rdap_cleans_hostile_and_malformed_values():
    info = parse_rdap(
        {
            "name": "evil\x00\nname" + "x" * 500,
            "country": "<script>",
            "cidr0_cidrs": [{"v4prefix": "<b>", "length": "x"}],
            "startAddress": "javascript:alert(1)",
            "entities": ["junk", {"roles": ["registrant"], "vcardArray": "nope"}, {"roles": None}],
            "remarks": "junk",
        }
    )
    assert "\x00" not in (info.network or "") and "\n" not in (info.network or "")
    assert len(info.network or "") <= 200
    assert info.country is None
    assert info.cidr is None
    assert info.organization is None


def test_parse_rdap_rejects_a_non_object():
    with pytest.raises(IpLookupError):
        parse_rdap(["not", "an", "object"])


def _service(fetch, **kwargs):
    defaults = {"enabled": True, "timeout": 1.0, "max_per_minute": 100}
    return IpInfoService(fetch=fetch, **{**defaults, **kwargs})


def test_private_addresses_are_never_sent_to_the_registry():
    calls = []
    service = _service(lambda ip, timeout: calls.append(ip) or ARIN)
    assert service.lookup(parse_ip("192.168.1.5")).status == "local"
    assert service.lookup(parse_ip("127.0.0.1")).status == "local"
    assert calls == []


def test_disabled_makes_no_outbound_request():
    calls = []
    service = _service(lambda ip, timeout: calls.append(ip) or ARIN, enabled=False)
    assert service.lookup(parse_ip("8.8.8.8")).status == "disabled"
    assert calls == []


def test_results_are_cached_so_the_registry_is_asked_once():
    calls = []
    service = _service(lambda ip, timeout: calls.append(ip) or ARIN)
    first = service.lookup(parse_ip("8.8.8.8"))
    second = service.lookup(parse_ip("8.8.8.8"))
    assert first.status == second.status == "ok"
    assert second.info and second.info.organization == "Google LLC"
    assert calls == ["8.8.8.8"]


def test_failures_are_cached_briefly_and_never_raise():
    calls = []

    def failing(ip, timeout):
        calls.append(ip)
        raise IpLookupError("boom")

    service = _service(failing)
    assert service.lookup(parse_ip("8.8.8.8")).status == "unavailable"
    assert service.lookup(parse_ip("8.8.8.8")).status == "unavailable"
    assert len(calls) == 1


def test_uncached_lookups_are_rate_limited_and_not_negatively_cached():
    calls = []
    service = _service(lambda ip, timeout: calls.append(ip) or ARIN, max_per_minute=2)
    assert service.lookup(parse_ip("8.8.8.8")).status == "ok"
    assert service.lookup(parse_ip("8.8.4.4")).status == "ok"
    assert service.lookup(parse_ip("1.1.1.1")).status == "unavailable"  # over the cap
    assert len(calls) == 2
    assert service.lookup(parse_ip("8.8.8.8")).status == "ok"  # cached answers still served


def test_cache_is_bounded():
    service = _service(lambda ip, timeout: ARIN, max_entries=2)
    for ip in ("8.8.8.1", "8.8.8.2", "8.8.8.3"):
        service.lookup(parse_ip(ip))
    assert len(service._cache) == 2


def test_redirects_only_go_to_rdap_https_hosts():
    handler = ip_info._RdapRedirects()
    req = urllib_request("https://rdap.org/ip/8.8.8.8")
    ok = handler.redirect_request(req, None, 302, "Found", {}, "https://rdap.arin.net/registry/ip/8.8.8.8")
    assert ok is not None
    for bad in (
        "http://rdap.arin.net/x",
        "https://evil.example/x",
        "https://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "https://notrdap.example/x",
    ):
        with pytest.raises(urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, bad)


def urllib_request(url):
    import urllib.request

    return urllib.request.Request(url)
