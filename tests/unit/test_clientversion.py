from __future__ import annotations

import pytest

from rustdesk_api.clientversion import is_older, parse_version


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.4.0", (1, 4, 0)),
        ("1.3.9-2", (1, 3, 9)),
        ("v1.2", (1, 2)),
        ("  1.4.9 (nightly)", (1, 4, 9)),
        ("", None),
        (None, None),
        ("nightly", None),
    ],
)
def test_parse_version(text, expected):
    assert parse_version(text) == expected


@pytest.mark.parametrize(
    ("version", "minimum", "older"),
    [
        ("1.3.9", "1.4.0", True),
        ("1.4.0", "1.4.0", False),
        ("1.4.1", "1.4.0", False),
        ("1.10.0", "1.9.9", False),  # numeric, not alphabetical
        ("1.4", "1.4.0", False),  # a missing part counts as zero
        ("1.4", "1.4.1", True),
        ("1.3.9-2", "1.4.0", True),
        # Nothing known about the client: not called outdated.
        (None, "1.4.0", False),
        ("garbage", "1.4.0", False),
        ("1.3.0", "", False),
    ],
)
def test_is_older(version, minimum, older):
    assert is_older(version, minimum) is older
