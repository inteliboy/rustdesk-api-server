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


def test_the_commit_baked_into_the_image_beats_a_stale_setting(monkeypatch, tmp_path):
    """A container manager can re-apply the previous image's GIT_COMMIT to a newer image."""
    stale = "3301ab6" + "0" * 33
    baked = tmp_path / "BUILD_COMMIT"
    baked.write_text(SHA)  # no trailing newline, as the Dockerfile writes it
    monkeypatch.setattr(buildinfo, "_BAKED_COMMIT_FILE", baked)
    assert get_build_info(stale).commit == SHA


def test_an_empty_or_missing_baked_file_falls_back_to_the_setting(monkeypatch, tmp_path):
    baked = tmp_path / "BUILD_COMMIT"
    monkeypatch.setattr(buildinfo, "_BAKED_COMMIT_FILE", baked)
    assert get_build_info(SHA).commit == SHA  # missing
    baked.write_text("")  # an image built without the build argument
    assert get_build_info(SHA).commit == SHA
    baked.write_text("not-a-sha")
    assert get_build_info(SHA).commit == SHA


def test_without_a_setting_the_checkout_is_asked(monkeypatch):
    monkeypatch.setattr(buildinfo, "_checkout_commit", lambda: (SHA, True))
    info = get_build_info("")
    assert info.commit == SHA
    assert info.dirty is True
