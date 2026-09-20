"""Basic WHOIS-style details (network, organisation, country, range) for public IPs.

Uses RDAP, the JSON successor to WHOIS: `https://rdap.org/ip/<ip>` redirects to
the regional registry (ARIN, RIPE, APNIC, LACNIC, AFRINIC) that owns the
address. Implemented with the standard library so it adds no dependency.

Privacy and safety, since this makes outbound requests on behalf of users:

- Only globally routable IP *literals* are ever queried. Private, loopback,
  link-local, CGNAT, multicast and reserved addresses never leave the process,
  and nothing but the validated address is sent to the registry.
- The queried host is fixed. A redirect is followed only to another
  `https://rdap.*` host, and the response size and time are capped.
- The registry's answer is third-party data. Every field is length-limited and
  stripped of control characters here, and the WebUI still escapes it.
- Results (and failures) are cached in memory, and uncached lookups are rate
  limited, so a busy page cannot turn into a stream of registry queries.
- Can be switched off entirely with `IP_LOOKUP_ENABLED=false`.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from rustdesk_api.security.rate_limit import RateLimiter

RDAP_BASE_URL = "https://rdap.org/ip/"
_MAX_BODY_BYTES = 512 * 1024
_FIELD_LIMIT = 200
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Status = Literal["ok", "local", "disabled", "unavailable"]


class IpLookupError(Exception):
    """The registry could not be queried or its answer was unusable."""


@dataclass(frozen=True)
class IpInfo:
    network: str | None = None
    organization: str | None = None
    country: str | None = None
    cidr: str | None = None
    registry: str | None = None


@dataclass(frozen=True)
class IpLookupResult:
    ip: str
    status: Status
    info: IpInfo | None = None


def parse_ip(value: str) -> IpAddress | None:
    """The address in `value`, or None if it is not an IP literal. An IPv6 zone
    ("%eth0") is dropped and an IPv4-mapped IPv6 address is unwrapped."""
    try:
        addr = ipaddress.ip_address(value.strip().split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def is_public_ip(addr: IpAddress) -> bool:
    return addr.is_global and not addr.is_multicast


def _clean(value: Any, limit: int = _FIELD_LIMIT) -> str | None:
    if not isinstance(value, str):
        return None
    text = _CONTROL_CHARS.sub(" ", value).strip()
    return text[:limit] or None


def _vcard_name(entity: dict[str, Any]) -> str | None:
    vcard = entity.get("vcardArray")
    if not (isinstance(vcard, list) and len(vcard) > 1 and isinstance(vcard[1], list)):
        return None
    for prop in vcard[1]:
        if isinstance(prop, list) and len(prop) > 3 and prop[0] == "fn":
            return _clean(prop[3])
    return None


def _organization(doc: dict[str, Any]) -> str | None:
    entities = doc.get("entities")
    for entity in entities if isinstance(entities, list) else []:
        if isinstance(entity, dict) and "registrant" in (entity.get("roles") or []):
            name = _vcard_name(entity)
            if name:
                return name
    # Some registries describe the owner only in free-text remarks.
    remarks = doc.get("remarks")
    for remark in remarks if isinstance(remarks, list) else []:
        lines = remark.get("description") if isinstance(remark, dict) else None
        if isinstance(lines, list) and lines:
            return _clean(lines[0])
    return None


def _cidr(doc: dict[str, Any]) -> str | None:
    blocks = doc.get("cidr0_cidrs")
    if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict):
        prefix = blocks[0].get("v4prefix") or blocks[0].get("v6prefix")
        length = blocks[0].get("length")
        try:
            return str(ipaddress.ip_network(f"{prefix}/{length}", strict=False))
        except ValueError:
            pass  # fall through to the start/end range
    try:
        start = ipaddress.ip_address(str(doc.get("startAddress")))
        end = ipaddress.ip_address(str(doc.get("endAddress")))
    except ValueError:
        return None
    return f"{start} - {end}"


def parse_rdap(doc: Any) -> IpInfo:
    """Reduces an RDAP `ip network` object to the few fields the WebUI shows."""
    if not isinstance(doc, dict):
        raise IpLookupError("unexpected RDAP response")
    country = _clean(doc.get("country"), 2)
    return IpInfo(
        network=_clean(doc.get("name")),
        organization=_organization(doc),
        country=country.upper() if country and country.isalpha() and len(country) == 2 else None,
        cidr=_cidr(doc),
        registry=_clean(doc.get("port43")),
    )


class _RdapRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects only to another https://rdap.* host (the registries)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or not (parts.hostname or "").startswith("rdap."):
            raise urllib.error.URLError("refusing to follow redirect to a non-RDAP host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def rdap_fetch(ip: str, timeout: float) -> Any:
    """Queries RDAP for one already-validated public IP literal."""
    request = urllib.request.Request(
        RDAP_BASE_URL + urllib.parse.quote(ip, safe=":."),
        headers={"Accept": "application/rdap+json", "User-Agent": "rustdesk-api-server"},
    )
    opener = urllib.request.build_opener(_RdapRedirects)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(_MAX_BODY_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise IpLookupError(f"RDAP request failed: {exc}") from exc
    if len(body) > _MAX_BODY_BYTES:
        raise IpLookupError("RDAP response too large")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise IpLookupError("RDAP response was not JSON") from exc


class IpInfoService:
    """One per application (kept on `app.state`): holds the cache and the
    outbound rate limit. `fetch` is injectable so tests never touch the network."""

    def __init__(
        self,
        *,
        enabled: bool,
        timeout: float,
        max_per_minute: int,
        fetch: Callable[[str, float], Any] = rdap_fetch,
        ttl: float = 24 * 3600,
        failure_ttl: float = 600,
        max_entries: int = 1000,
    ) -> None:
        self._enabled = enabled
        self._timeout = timeout
        self._fetch = fetch
        self._ttl = ttl
        self._failure_ttl = failure_ttl
        self._max_entries = max_entries
        self._limiter = RateLimiter(max_attempts=max_per_minute, window_seconds=60)
        self._cache: OrderedDict[str, tuple[float, IpInfo | None]] = OrderedDict()
        self._lock = threading.Lock()

    def lookup(self, addr: IpAddress) -> IpLookupResult:
        ip = str(addr)
        if not is_public_ip(addr):
            return IpLookupResult(ip, "local")
        if not self._enabled:
            return IpLookupResult(ip, "disabled")

        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(ip)
            if cached is not None and cached[0] > now:
                self._cache.move_to_end(ip)
                return self._result(ip, cached[1])

        if not self._limiter.allow("outbound"):
            return IpLookupResult(ip, "unavailable")  # not cached: retry later
        try:
            info: IpInfo | None = parse_rdap(self._fetch(ip, self._timeout))
            expires = now + self._ttl
        except IpLookupError:
            info, expires = None, now + self._failure_ttl
        with self._lock:
            self._cache[ip] = (expires, info)
            self._cache.move_to_end(ip)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
        return self._result(ip, info)

    @staticmethod
    def _result(ip: str, info: IpInfo | None) -> IpLookupResult:
        return IpLookupResult(ip, "ok", info) if info else IpLookupResult(ip, "unavailable")
