"""Time-based one-time passwords (RFC 6238, HMAC-SHA1, 6 digits, 30 s steps) - the
scheme every authenticator app implements by default.

Standard library only: the algorithm is a few lines of `hmac`, so it needs no
dependency. Nothing here stores anything; see services/two_factor.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
# Codes from one step either side are accepted (a phone clock a few seconds off,
# or a code typed just as it rolls over).
WINDOW_STEPS = 1


def generate_secret() -> str:
    """160 random bits, base32 (the form authenticator apps take)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def _code_for_step(secret: str, step: int) -> str:
    key = base64.b32decode(secret, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def code_at(secret: str, now: float | None = None) -> str:
    """The code an authenticator shows at `now` (tests and tooling only)."""
    return _code_for_step(secret, current_step(now))


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def normalize_code(code: str) -> str:
    return "".join(code.split())


def is_totp_shaped(code: str) -> bool:
    text = normalize_code(code)
    # ASCII only: str.isdigit() also accepts other scripts' digits, which
    # hmac.compare_digest cannot compare.
    return len(text) == DIGITS and text.isascii() and text.isdigit()


def verify(secret: str, code: str, *, last_step: int | None = None, now: float | None = None) -> int | None:
    """The time step `code` is valid for, or `None`. A step at or before
    `last_step` (one already accepted) is refused, so a code cannot be used
    twice even inside its window."""
    text = normalize_code(code)
    if not is_totp_shaped(text):
        return None
    current = current_step(now)
    matched: int | None = None
    # Every candidate is compared (no early exit) so timing says nothing about
    # which step, if any, was close.
    for step in range(current - WINDOW_STEPS, current + WINDOW_STEPS + 1):
        if hmac.compare_digest(_code_for_step(secret, step), text) and matched is None:
            matched = step
    if matched is None or (last_step is not None and matched <= last_step):
        return None
    return matched


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    """The `otpauth://` link authenticator apps read (usually from a QR code)."""
    label = f"{quote(issuer, safe='')}:{quote(account, safe='')}"
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )
