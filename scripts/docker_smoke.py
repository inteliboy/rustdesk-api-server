"""Smoke test for a running RustDesk API server, meant for a Docker container.

It talks to the server over HTTP only and needs nothing but the Python standard library, so it runs the
same way on Windows, Linux and macOS and in CI.

    python scripts/docker_smoke.py first    # fresh server: set up the admin, register a device
    (restart the container, or `docker compose down` and `up` again)
    python scripts/docker_smoke.py again    # same data volume: the admin and the device are still there

Options: --url (default http://127.0.0.1:21114), --wait (seconds to wait for /health, default 90).
The admin it creates is "smoke-admin" with a throwaway password; run it against a disposable
container, not a server you use.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

ADMIN = "smoke-admin"
PASSWORD = "smoke-test-password-1"  # throwaway credential for a disposable container
DEVICE_ID = "987654321"

failures: list[str] = []


def call(base: str, path: str, *, body: dict | None = None, token: str | None = None) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("ok   " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(label)
    return ok


def wait_until_healthy(base: str, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if call(base, "/health")[0] == 200:
                return True
        except OSError:
            pass  # the container is still starting; try again until the deadline
        time.sleep(2)
    return False


def login(base: str) -> str | None:
    """Sign in the way the RustDesk client does and return its bearer token."""
    status, raw = call(base, "/api/login", body={"username": ADMIN, "password": PASSWORD})
    token = json.loads(raw).get("access_token") if status == 200 else None
    check("client login (/api/login)", bool(token), str(status))
    return token


def device_listed(base: str, token: str) -> bool:
    status, raw = call(base, "/api/v1/devices?page_size=100", token=token)
    ids = [item["rustdesk_id"] for item in json.loads(raw).get("items", [])] if status == 200 else []
    return DEVICE_ID in ids


def first_run(base: str) -> None:
    status, raw = call(base, "/api/v1/auth/setup/status")
    check("fresh server asks for setup", status == 200 and json.loads(raw)["setup_required"] is True)

    status, _ = call(base, "/api/v1/auth/setup", body={"username": ADMIN, "password": PASSWORD})
    check("first administrator created", status in (200, 201), str(status))
    status, _ = call(base, "/api/v1/auth/setup", body={"username": "second", "password": PASSWORD})
    check("setup is closed afterwards", status == 403, str(status))

    token = login(base)
    if not token:
        return
    info = {"id": DEVICE_ID, "hostname": "smoke-host", "os": "linux / Debian 12", "version": "1.4.9"}
    status, _ = call(base, "/api/sysinfo", body=info, token=token)
    check("device registers (/api/sysinfo)", status == 200, str(status))
    status, raw = call(base, "/api/heartbeat", body={"id": DEVICE_ID})
    check("heartbeat answered", status == 200 and json.loads(raw).get("data") == "OK", raw.decode()[:60])
    check("device appears in the device list", device_listed(base, token))
    status, raw = call(base, "/api/v1/admin/installer", token=token)
    check(
        "the image has NSIS (installer builder available)",
        status == 200 and json.loads(raw)["available"] is True,
        raw.decode()[:80],
    )


def later_run(base: str) -> None:
    status, raw = call(base, "/api/v1/auth/setup/status")
    setup_offered = status != 200 or json.loads(raw)["setup_required"]
    check("data survived: setup not offered again", not setup_offered)
    token = login(base)
    if token:
        check("data survived: device still listed", device_listed(base, token))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("phase", choices=["first", "again"])
    parser.add_argument("--url", default="http://127.0.0.1:21114")
    parser.add_argument("--wait", type=int, default=90)
    args = parser.parse_args()
    base = args.url.rstrip("/")

    if not check(f"server answers /health at {base}", wait_until_healthy(base, args.wait)):
        return 1
    status, raw = call(base, "/ready")
    ready = status == 200 and json.loads(raw).get("database") == "ok"
    check("ready (database reachable)", ready, raw.decode()[:60])
    status, raw = call(base, "/api/version")
    check("version endpoint", status == 200 and "version" in json.loads(raw), raw.decode()[:60])
    status, raw = call(base, "/")
    check("WebUI page loads", status == 200 and b"<html" in raw.lower(), str(status))
    status, raw = call(base, "/static/css/tailwind.css")
    check("packaged stylesheet is served", status == 200 and len(raw) > 10_000, f"{status}, {len(raw)} bytes")
    status, _ = call(base, "/api/v1/devices")
    check("management API refuses anonymous callers", status == 401, str(status))

    first_run(base) if args.phase == "first" else later_run(base)

    print(f"\n{len(failures)} check(s) failed" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
