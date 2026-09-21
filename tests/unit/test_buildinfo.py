"""Given a configured commit (Docker) or a git checkout, the build info names the commit and links to it."""

from rustdesk_api import buildinfo
from rustdesk_api.buildinfo import REPOSITORY_URL, RUSTDESK_CLIENT_SOURCE, get_build_info

SHA = "f81a8e8" + "0" * 33


def test_a_configured_commit_is_used_and_linked():
    info = get_build_info(SHA.upper())  # case and whitespace do not matter
    assert info.commit == SHA
    assert info.commit_url == f"{REPOSITORY_URL}/commit/{SHA}"
    assert info.dirty is False
    assert info.as_dict()["commit_short"] == "f81a8e8"
    assert info.rustdesk_client_source == RUSTDESK_CLIENT_SOURCE


def test_a_value_that_is_not_a_hash_is_ignored_not_linked(monkeypatch):
    monkeypatch.setattr(buildinfo, "_checkout_commit", lambda: (None, False))
    for bad in ("unknown", "not-a-sha", "../../etc", "f81a8e8; rm -rf /"):
        info = get_build_info(bad)
        assert info.commit is None
        assert info.commit_url is None


def test_without_a_setting_the_checkout_is_asked(monkeypatch):
    monkeypatch.setattr(buildinfo, "_checkout_commit", lambda: (SHA, True))
    info = get_build_info("")
    assert info.commit == SHA
    assert info.dirty is True
