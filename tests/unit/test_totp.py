"""RFC 6238 time-based one-time passwords."""

import base64

import pytest

from rustdesk_api.security import totp

# RFC 6238 appendix B: the SHA-1 secret is the ASCII string "12345678901234567890";
# the published codes have 8 digits, ours 6 - the last six of the same number.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
        (20000000000, "353130"),
    ],
)
def test_matches_the_rfc_test_vectors(timestamp, expected):
    assert totp.code_at(RFC_SECRET, timestamp) == expected


def test_generated_secrets_are_random_base32_of_160_bits():
    first, second = totp.generate_secret(), totp.generate_secret()
    assert first != second
    assert len(base64.b32decode(first)) == 20


def test_a_code_is_accepted_one_step_either_side_but_not_two():
    secret = totp.generate_secret()
    now = 1_700_000_000
    code = totp.code_at(secret, now)
    assert totp.verify(secret, code, now=now) == totp.current_step(now)
    assert totp.verify(secret, code, now=now + 30) == totp.current_step(now)
    assert totp.verify(secret, code, now=now - 30) == totp.current_step(now)
    assert totp.verify(secret, code, now=now + 90) is None
    assert totp.verify(secret, code, now=now - 90) is None


def test_a_step_that_was_already_used_is_refused():
    secret = totp.generate_secret()
    now = 1_700_000_000
    step = totp.verify(secret, totp.code_at(secret, now), now=now)
    assert step is not None
    assert totp.verify(secret, totp.code_at(secret, now), last_step=step, now=now) is None
    # A later step is fine.
    later = totp.code_at(secret, now + 30)
    assert totp.verify(secret, later, last_step=step, now=now + 30) == step + 1


@pytest.mark.parametrize("junk", ["", "12345", "1234567", "abcdef", "12 34", "١٢٣٤٥٦", "12345 6 7"])
def test_malformed_codes_never_verify(junk):
    assert totp.verify(totp.generate_secret(), junk) is None


def test_spaces_in_a_code_are_ignored():
    secret = totp.generate_secret()
    now = 1_700_000_000
    code = totp.code_at(secret, now)
    assert totp.verify(secret, f"{code[:3]} {code[3:]}", now=now) is not None


def test_the_provisioning_uri_is_what_authenticator_apps_read():
    uri = totp.provisioning_uri("ABCDEFGH", "alice@example.com", "RustDesk API Server")
    assert uri.startswith("otpauth://totp/RustDesk%20API%20Server:alice%40example.com?")
    assert "secret=ABCDEFGH" in uri and "issuer=RustDesk%20API%20Server" in uri
    assert "digits=6" in uri and "period=30" in uri
