"""Encryption at rest for the few secrets this server must be able to read back
(CLAUDE.md section 18) - today only a shared address book's connection
passwords, which the RustDesk client sends in clear and expects to receive again.

Fernet (AES-128-CBC + HMAC-SHA256, from `cryptography`). The key is
`DATA_ENCRYPTION_KEY`, deliberately separate from `SECRET_KEY`: rotating the
session secret must not make stored passwords unreadable. Several keys may be
given, comma separated - the first encrypts, every one can decrypt - which is
how a key is rotated (`rustdesk-api rotate-data-key` re-encrypts the rows).

Nothing here logs or returns key material or plaintext.
"""

from __future__ import annotations

import functools

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


def parse_keys(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def validate_keys(raw: str) -> None:
    """Raises `ValueError` (without echoing the value) unless every entry is a
    well-formed Fernet key."""
    for index, key in enumerate(parse_keys(raw), start=1):
        try:
            Fernet(key)
        except (ValueError, TypeError):
            raise ValueError(
                f"DATA_ENCRYPTION_KEY entry {index} is not a valid key. Generate one with "
                "`rustdesk-api generate-key`."
            ) from None


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


class SecretBox:
    def __init__(self, keys: list[str]) -> None:
        self._fernet = MultiFernet([Fernet(key) for key in keys])

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str | None:
        """`None` when no configured key opens the token (a lost or changed
        key, or a corrupted row) - the caller decides how to degrade."""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return None

    def rotate(self, token: str) -> str | None:
        """Re-encrypts with the primary key; `None` when it cannot be opened."""
        try:
            return self._fernet.rotate(token.encode("ascii")).decode("ascii")
        except (InvalidToken, ValueError):
            return None


@functools.lru_cache(maxsize=4)
def _box_for(raw_keys: str) -> SecretBox | None:
    keys = parse_keys(raw_keys)
    return SecretBox(keys) if keys else None


def get_secret_box(raw_keys: str) -> SecretBox | None:
    """`None` when no key is configured - features that need one then stay off
    rather than falling back to storing secrets in clear."""
    return _box_for(raw_keys)
