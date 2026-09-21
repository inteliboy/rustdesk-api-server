"""RustDesk client version numbers ("1.4.0", "1.3.9-2", "1.2.3 (nightly)")."""

from __future__ import annotations

import re

_NUMBERS = re.compile(r"^\s*v?(\d+(?:\.\d+){0,3})")


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """The leading dotted numbers of a version, or None when there are none.

    Anything after them (a build suffix, a channel name) is ignored: the server only
    needs to know whether a client is older than a minimum, not to order builds of
    the same release."""
    if not text:
        return None
    match = _NUMBERS.match(text)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_older(version: str | None, minimum: str | None) -> bool:
    """True only when both are understood and `version` is below `minimum`. A client
    that reports no (or an unreadable) version is not called outdated: nothing is
    known about it."""
    have, need = parse_version(version), parse_version(minimum)
    if have is None or need is None:
        return False
    width = max(len(have), len(need))
    return have + (0,) * (width - len(have)) < need + (0,) * (width - len(need))
