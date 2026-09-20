"""WEBUI_ALLOWED_NETWORKS: which client addresses may use the management
interface (the WebUI pages, /api/v1 and its WebSocket)."""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence

# Reachable from anywhere whatever the list says: the health probes, the
# Prometheus scrape (it has its own token) and the RustDesk client protocol
# (`/api/...` outside `/api/v1`) - clients and monitors connect from anywhere.
_ALWAYS_OPEN_PATHS = frozenset({"/health", "/ready", "/metrics"})


def is_restricted_path(path: str) -> bool:
    if path in _ALWAYS_OPEN_PATHS:
        return False
    return not (path.startswith("/api/") and not path.startswith("/api/v1/"))


def is_allowed(
    address: str | None, networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> bool:
    """True if `address` is inside one of `networks`. Fails closed: an address
    that is missing or cannot be parsed is refused."""
    try:
        parsed = ipaddress.ip_address(address or "")
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return any(parsed.version == net.version and parsed in net for net in networks)
