"""The server settings a RustDesk client needs, in the forms the client accepts.

Formats verified against the client source (`ServerConfig` in flutter/lib/common.dart and
`CustomServer` in src/custom_server.rs):

* the **config string**: JSON `{"host", "relay", "api", "key"}` as URL-safe base64, then
  reversed. `rustdesk --config <string>` (installed client, as administrator/root), the
  Android/desktop "ID/Relay server" dialog (paste or scan a QR code) and `rustdesk://config/<string>`
  on mobile all take it. The decoders accept it with or without the `=` padding.
* the **file name**: a Windows executable renamed `rustdesk-host=<id>,key=<key>,api=<api>,relay=<relay>.exe`
  configures itself on first start.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

MAX_FIELD = 255
MAX_KEY = 255


@dataclass(frozen=True)
class ClientServers:
    id_server: str
    relay_server: str = ""
    api_server: str = ""
    key: str = ""

    def as_json(self) -> str:
        # The order the client itself writes.
        return json.dumps(
            {"host": self.id_server, "relay": self.relay_server, "api": self.api_server, "key": self.key},
            separators=(",", ":"),
        )


def clean(value: str | None, limit: int = MAX_FIELD) -> str:
    return (value or "").strip()[:limit]


def config_string(servers: ClientServers) -> str:
    encoded = base64.urlsafe_b64encode(servers.as_json().encode()).decode().rstrip("=")
    return encoded[::-1]


def decode_config_string(text: str) -> dict:
    """The inverse, for tests and for checking a string someone pastes."""
    reversed_text = text.strip()[::-1]
    reversed_text += "=" * (-len(reversed_text) % 4)
    return json.loads(base64.urlsafe_b64decode(reversed_text))


def exe_name(servers: ClientServers) -> str:
    """Only the parts that are set; a comma ends the name so Windows' " (1)" suffix on a
    duplicate download cannot garble the last value."""
    parts = [f"host={servers.id_server}"]
    if servers.key:
        parts.append(f"key={servers.key}")
    if servers.api_server:
        parts.append(f"api={servers.api_server}")
    if servers.relay_server:
        parts.append(f"relay={servers.relay_server}")
    return "rustdesk-" + ",".join(parts) + ",.exe"


def warnings(servers: ClientServers) -> list[str]:
    """Settings that are accepted but will not do what the person expects."""
    found = []
    api = servers.api_server
    if api.lower().startswith("https://") and api.rstrip("/").endswith(":21114"):
        found.append("https_21114")  # the client silently drops ":21114" from an https address
    if api and not api.lower().startswith(("http://", "https://")):
        found.append("api_without_scheme")  # the client needs the http:// or https://
    if not servers.key:
        found.append("no_key")
    return found
