"""Cryptographically random token generation and hashing.

Raw session/API tokens are only ever returned to the caller at creation
time. Only a SHA-256 hash of the token is persisted, so a stolen database
dump cannot be used to impersonate sessions directly.
"""

from __future__ import annotations

import hashlib
import secrets

TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 32


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    return secrets.compare_digest(a, b)
