"""Minimal User-Agent summarizer for audit entries.

Deliberately small and dependency-free: it only has to turn a header into
something an administrator can read ("Chrome 130 on Windows"). The header is
client-controlled, so the summary is built from a fixed vocabulary of names
plus a numeric major version - never from arbitrary header text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Order matters: many browsers also advertise "Chrome/" and "Safari/".
_BROWSERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Edge", re.compile(r"\bEdg(?:e|A|iOS)?/(\d+)")),
    ("Opera", re.compile(r"\bOPR/(\d+)")),
    ("Firefox", re.compile(r"\b(?:Firefox|FxiOS)/(\d+)")),
    ("Chrome", re.compile(r"\b(?:Chrome|CriOS)/(\d+)")),
    ("Safari", re.compile(r"\bVersion/(\d+)[^ ]* (?:Mobile/\S+ )?Safari/")),
)

# Non-browser HTTP clients, matched on the leading product token.
_TOOLS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("RustDesk client", re.compile(r"^(?:rustdesk|reqwest)", re.IGNORECASE)),
    ("curl", re.compile(r"^curl/", re.IGNORECASE)),
    ("python-requests", re.compile(r"^python-requests/", re.IGNORECASE)),
    ("Python", re.compile(r"^(?:python-urllib|python-httpx|Python/)", re.IGNORECASE)),
)

# Android must precede Linux (Android UAs contain "Linux"), iOS must precede
# macOS (iOS UAs contain "like Mac OS X").
_SYSTEMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Android", re.compile(r"Android")),
    ("iOS", re.compile(r"iPhone|iPad|iPod")),
    ("Windows", re.compile(r"Windows")),
    ("macOS", re.compile(r"Macintosh|Mac OS X")),
    ("ChromeOS", re.compile(r"CrOS")),
    ("Linux", re.compile(r"Linux|X11")),
)


@dataclass(frozen=True)
class UserAgentInfo:
    browser: str | None
    os: str | None


def summarize_user_agent(user_agent: str | None) -> UserAgentInfo:
    """Returns e.g. UserAgentInfo("Chrome 130", "Windows"). Unrecognized
    parts are None rather than guessed."""
    if not user_agent:
        return UserAgentInfo(None, None)

    browser: str | None = None
    for name, pattern in _TOOLS:
        if pattern.search(user_agent):
            browser = name
            break
    if browser is None:
        for name, pattern in _BROWSERS:
            match = pattern.search(user_agent)
            if match:
                browser = f"{name} {match.group(1)}"
                break

    os_name: str | None = None
    for name, pattern in _SYSTEMS:
        if pattern.search(user_agent):
            os_name = name
            break
    return UserAgentInfo(browser, os_name)


def audit_client_detail(user_agent: str | None, *, max_raw_length: int = 200) -> dict[str, str]:
    """Fields to merge into an audit entry's `detail`. Only non-empty values
    are returned. The raw header is kept truncated for forensics; it is
    attacker-controlled (failed logins are unauthenticated), so consumers
    must escape it."""
    info = summarize_user_agent(user_agent)
    detail: dict[str, str] = {}
    if info.browser:
        detail["browser"] = info.browser
    if info.os:
        detail["os"] = info.os
    if user_agent:
        detail["user_agent"] = user_agent[:max_raw_length]
    return detail
