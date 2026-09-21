"""The Windows installer builder: release parsing, the NSIS script, downloads and the kit."""

from __future__ import annotations

import hashlib
import io
import re
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from rustdesk_api.config import Settings
from rustdesk_api.services import client_config
from rustdesk_api.services import installer as inst

MSI = b"fake-msi-bytes" * 100
MSI_SHA = hashlib.sha256(MSI).hexdigest()
SERVERS = client_config.ClientServers(
    "id.example.com", "relay.example.com", "https://api.example.com", "pubkey="
)


def _asset(name, sha=MSI_SHA, tag="1.4.9"):
    return {
        "name": name,
        "size": len(MSI),
        "digest": f"sha256:{sha}" if sha else None,
        "browser_download_url": f"https://github.com/rustdesk/rustdesk/releases/download/{tag}/{name}",
    }


def _payload():
    return [
        {
            "tag_name": "nightly",
            "prerelease": True,
            "draft": False,
            "published_at": "2026-07-10T05:02:29Z",
            "assets": [
                _asset("rustdesk-1.5.0-x86_64.msi", tag="nightly"),
                _asset("rustdesk-1.5.0-aarch64.msi", tag="nightly"),
            ],
        },
        {
            "tag_name": "1.4.9",
            "prerelease": False,
            "draft": False,
            "published_at": "2026-07-06T10:02:30Z",
            "assets": [_asset("rustdesk-1.4.9-x86_64.msi"), {"name": "rustdesk-1.4.9-x86_64.exe", "size": 5}],
        },
        {"tag_name": "1.4.0", "draft": True, "assets": [_asset("rustdesk-1.4.0-x86_64.msi")]},
        {"tag_name": "1.3.0", "prerelease": False, "assets": [{"name": "rustdesk-1.3.0.dmg"}]},
    ]


# --- releases -------------------------------------------------------------


def test_releases_keep_only_those_with_a_windows_msi_and_map_the_architectures():
    releases = inst.parse_releases(_payload())

    assert [r.tag for r in releases] == [
        "nightly",
        "1.4.9",
    ]  # the draft and the release without an MSI are gone
    nightly, stable = releases
    assert nightly.prerelease is True and nightly.version == "1.5.0"
    assert sorted(nightly.assets) == ["arm64", "x64"]
    assert nightly.assets["arm64"].name == "rustdesk-1.5.0-aarch64.msi"
    assert nightly.assets["x64"].sha256 == MSI_SHA
    assert sorted(stable.assets) == ["x64"]  # older releases have no ARM64 build


def test_an_asset_that_is_not_on_githubs_release_downloads_is_ignored():
    payload = _payload()[1:2]
    payload[0]["assets"][0]["browser_download_url"] = (
        "https://evil.example.com/rustdesk/rustdesk/releases/download/x.msi"
    )
    assert inst.parse_releases(payload) == []
    payload[0]["assets"][0]["browser_download_url"] = (
        "http://github.com/rustdesk/rustdesk/releases/download/1.4.9/x.msi"
    )
    assert inst.parse_releases(payload) == []
    payload[0]["assets"][0]["browser_download_url"] = (
        "https://github.com/someone/else/releases/download/1.4.9/x.msi"
    )
    assert inst.parse_releases(payload) == []


def test_odd_tags_and_file_names_are_skipped_rather_than_trusted():
    payload = [
        {"tag_name": '1.0"; calc', "assets": [_asset("rustdesk-1.0-x86_64.msi")]},
        {"tag_name": "1.2.3", "assets": [_asset('rustdesk-1.2"$3-x86_64.msi')]},
    ]
    assert inst.parse_releases(payload) == []
    with pytest.raises(inst.InstallerError):
        inst.parse_releases({"message": "rate limited"})


def test_a_missing_digest_is_allowed_but_not_trusted_as_a_cache_key():
    payload = _payload()[1:2]
    payload[0]["assets"][0]["digest"] = None
    assert inst.parse_releases(payload)[0].assets["x64"].sha256 is None


# --- the NSIS script ------------------------------------------------------


def _spec(**changes):
    values = {
        "servers": SERVERS,
        "version": "1.4.9",
        "arch": "x64",
        "msi": r"C:\cache\rustdesk.msi",
        "icon": r"C:\cache\rustdesk.ico",
        "outfile": r"C:\out\setup.exe",
    }
    values.update(changes)
    return inst.ScriptSpec(**values)


def test_the_script_installs_the_msi_and_applies_the_config_string():
    text = inst.render_nsi(_spec())

    config = client_config.config_string(SERVERS)
    assert f'!define CONFIG_ARGS "--config {config}"' in text
    assert 'File /oname=$TEMP\\${MSI_FILE} "C:\\cache\\rustdesk.msi"' in text
    assert 'Outfile "C:\\out\\setup.exe"' in text
    assert "msiexec /i" in text and "--install-service" in text and "--uninstall" in text
    assert "SilentInstall silent" in text and "RequestExecutionLevel admin" in text
    assert '!if /FileExists "C:\\cache\\rustdesk.ico"' in text and 'Icon "C:\\cache\\rustdesk.ico"' in text
    # Nothing was left unfilled, and by default nothing is wiped or signed.
    assert "@@" not in text
    assert "RMDir" not in text and "!finalize" not in text


def test_the_existing_settings_are_only_wiped_when_asked_for():
    assert "RMDir" not in inst.render_nsi(_spec())
    assert text_has_wipe(inst.render_nsi(_spec(reset_settings=True)))


def text_has_wipe(text: str) -> bool:
    return all(
        p in text
        for p in ('RMDir /r "$APPDATA\\RustDesk"', 'RMDir /r "$LOCALAPPDATA\\rustdesk"', "ServiceProfiles")
    )


def test_no_icon_line_without_an_icon():
    assert "Icon" not in inst.render_nsi(_spec(icon=""))


def test_the_signing_command_becomes_a_finalize_step_with_dollars_escaped():
    text = inst.render_nsi(_spec(sign_command='signtool sign /sha1 ABC /fd SHA256 "%1" $HOME'))

    assert '!finalize `signtool sign /sha1 ABC /fd SHA256 "%1" $$HOME`' in text


def test_a_dollar_or_quote_in_a_path_cannot_break_out_of_the_string():
    text = inst.render_nsi(_spec(msi='C:\\a$b"c\\r.msi', outfile="C:\\$out.exe"))

    assert 'File /oname=$TEMP\\${MSI_FILE} "C:\\a$$b$\\"c\\r.msi"' in text
    assert 'Outfile "C:\\$$out.exe"' in text


def test_an_unexpected_version_or_architecture_is_refused():
    with pytest.raises(inst.InstallerError):
        inst.render_nsi(_spec(version='1.0"\nExecWait calc'))
    with pytest.raises(inst.InstallerError):
        inst.render_nsi(_spec(arch="mips"))


def test_settings_refuse_a_signing_command_that_cannot_be_written_into_a_script():
    for bad in ("signtool sign file.exe", "signtool `x` %1", "signtool %1\nExecWait calc"):
        with pytest.raises(ValueError):
            Settings(INSTALLER_SIGN_COMMAND=bad, _env_file=None)
    assert Settings(INSTALLER_SIGN_COMMAND='signtool sign "%1"', _env_file=None).installer_sign_command


def test_the_signing_command_is_not_in_the_settings_repr():
    settings = Settings(INSTALLER_SIGN_COMMAND='signtool sign /sha1 SECRETTHUMB "%1"', _env_file=None)
    assert "SECRETTHUMB" not in repr(settings)


# --- downloads ------------------------------------------------------------


@pytest.fixture()
def github(monkeypatch):
    """An in-process GitHub: the release list, the MSI (behind a redirect, as on the real
    site) and the icon. `calls` records what was requested."""
    calls: list[str] = []
    state = {"msi": MSI, "status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == "api.github.com":
            return httpx.Response(200, json=_payload())
        if host == "github.com" and path.endswith(".msi"):
            return httpx.Response(
                302, headers={"location": "https://release-assets.githubusercontent.com/blob" + path}
            )
        if host == "release-assets.githubusercontent.com":
            return httpx.Response(state["status"], content=state["msi"])
        if host == "raw.githubusercontent.com" and path.endswith("/res/icon.ico"):
            return httpx.Response(200, content=b"\x00\x00\x01\x00" + b"\x00" * 40)
        return httpx.Response(404)

    inst.set_transport(httpx.MockTransport(handler))
    yield calls, state
    inst.set_transport(None)
    inst.reset_state()


def _settings(tmp_path: Path, **extra) -> Settings:
    return Settings(DATABASE_URL=f"sqlite:///{(tmp_path / 'x.db').as_posix()}", _env_file=None, **extra)


def test_the_msi_is_downloaded_once_and_checked_against_githubs_digest(tmp_path, github):
    calls, _ = github
    settings = _settings(tmp_path)
    asset = inst.find_release("1.4.9").assets["x64"]

    first = inst.fetch_msi(settings, asset)
    second = inst.fetch_msi(settings, asset)

    assert first == second and first.read_bytes() == MSI
    assert sum(1 for c in calls if c.endswith(".msi") or "blob" in c) == 2  # the redirect and the file, once
    assert not list(first.parent.glob("*.partial"))


def test_a_download_that_does_not_match_the_digest_is_discarded(tmp_path, github):
    _, state = github
    state["msi"] = b"something else"
    settings = _settings(tmp_path)

    with pytest.raises(inst.InstallerError, match="checksum"):
        inst.fetch_msi(settings, inst.find_release("1.4.9").assets["x64"])

    assert not list((tmp_path / "installers" / "msi").glob("*.msi*"))


def test_a_failed_download_is_an_error_not_a_crash(tmp_path, github):
    _, state = github
    state["status"] = 500
    with pytest.raises(inst.InstallerError):
        inst.fetch_msi(_settings(tmp_path), inst.find_release("1.4.9").assets["x64"])


def test_a_download_larger_than_the_limit_is_stopped(tmp_path, github, monkeypatch):
    monkeypatch.setattr(inst, "MAX_MSI_BYTES", 100)
    with pytest.raises(inst.InstallerError, match="larger"):
        inst.fetch_msi(_settings(tmp_path), inst.find_release("1.4.9").assets["x64"])


def test_old_downloads_are_pruned(tmp_path, github, monkeypatch):
    monkeypatch.setattr(inst, "KEEP_MSI_FILES", 1)
    settings = _settings(tmp_path)
    inst.fetch_msi(settings, inst.find_release("1.4.9").assets["x64"])
    time.sleep(0.05)
    inst.fetch_msi(settings, inst.find_release("nightly").assets["x64"])

    assert len(list((tmp_path / "installers" / "msi").glob("*.msi"))) == 1


def test_the_icon_comes_from_the_release_and_is_optional(tmp_path, github):
    settings = _settings(tmp_path)
    icon = inst.fetch_icon(settings, "1.4.9")
    assert icon is not None and icon.read_bytes()[:4] == b"\x00\x00\x01\x00"

    # A configured icon wins; one that is not an icon is ignored (no icon, not a failed build).
    mine = tmp_path / "mine.ico"
    mine.write_bytes(b"\x00\x00\x01\x00" + b"1" * 40)
    assert inst.fetch_icon(_settings(tmp_path, INSTALLER_ICON=str(mine)), "1.4.9") == mine
    mine.write_bytes(b"not an icon at all, sorry")
    assert inst.fetch_icon(_settings(tmp_path, INSTALLER_ICON=str(mine)), "1.4.9") is None


def test_the_release_list_is_cached_and_survives_github_being_down(tmp_path, github, monkeypatch):
    calls, _ = github
    inst.list_releases()
    inst.list_releases()
    assert sum("api.github.com" in c for c in calls) == 1

    # Once the cache is stale a failing GitHub still gives the last list.
    monkeypatch.setattr(inst, "RELEASES_TTL_SECONDS", 0)
    inst.set_transport(httpx.MockTransport(lambda request: httpx.Response(503)))
    inst._releases_cache = (0.0, inst.parse_releases(_payload()))  # noqa: SLF001
    assert [r.tag for r in inst.list_releases()] == ["nightly", "1.4.9"]


def test_without_a_cached_list_a_dead_github_is_an_error(github):
    inst.set_transport(httpx.MockTransport(lambda request: httpx.Response(503)))
    with pytest.raises(inst.InstallerError):
        inst.list_releases()


# --- running makensis -----------------------------------------------------


class _Completed:
    def __init__(self, code, out=b"", err=b""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_makensis_output_never_carries_the_signing_command(tmp_path, monkeypatch):
    secret = 'signtool sign /sha1 SECRETTHUMBPRINT "%1"'
    monkeypatch.setattr(
        inst.subprocess,
        "run",
        lambda *a, **k: _Completed(1, f"Executing finalize: {secret}\nError: it broke\n".encode()),
    )
    with pytest.raises(inst.InstallerError) as raised:
        inst.run_makensis("makensis", tmp_path / "a.nsi", secret)

    assert "SECRETTHUMBPRINT" not in str(raised.value)
    assert "it broke" in str(raised.value) and "INSTALLER_SIGN_COMMAND" in str(raised.value)


def test_makensis_is_run_without_a_shell_in_the_scripts_folder(tmp_path, monkeypatch):
    seen = {}

    def fake(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return _Completed(0, b"Total size: 1\n")

    monkeypatch.setattr(inst.subprocess, "run", fake)
    inst.run_makensis("C:/NSIS/makensis.exe", tmp_path / "a.nsi")

    assert seen["argv"] == ["C:/NSIS/makensis.exe", "-V2", str(tmp_path / "a.nsi")]
    assert not seen.get("shell") and seen["cwd"] == tmp_path and seen["timeout"] == inst.BUILD_TIMEOUT_SECONDS


def test_a_missing_makensis_is_reported(tmp_path):
    with pytest.raises(inst.InstallerError):
        inst.run_makensis(str(tmp_path / "nothing-here"), tmp_path / "a.nsi")


def test_availability_says_why_it_cannot_build(tmp_path, monkeypatch):
    monkeypatch.setattr(inst.shutil, "which", lambda name: None)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    assert inst.availability(_settings(tmp_path)) == "no_makensis"
    assert inst.availability(_settings(tmp_path, INSTALLER_BUILD_ENABLED="false")) == "disabled"
    fake = tmp_path / "makensis.exe"
    fake.write_bytes(b"x")
    assert inst.availability(_settings(tmp_path, INSTALLER_MAKENSIS=str(fake))) is None


# --- the build kit --------------------------------------------------------


def test_the_kit_has_the_script_the_builder_and_instructions_and_nothing_secret():
    release = inst.parse_releases(_payload())[0]  # nightly 1.5.0
    kit = inst.build_kit(SERVERS, release, "arm64")

    with zipfile.ZipFile(io.BytesIO(kit)) as archive:
        assert sorted(archive.namelist()) == ["README.txt", "build.ps1", "rustdesk.nsi"]
        files = {name: archive.read(name).decode("ascii") for name in archive.namelist()}
    assert all("\r\n" in text and "@@" not in text for text in files.values())
    nsi, ps1 = files["rustdesk.nsi"], files["build.ps1"]
    assert client_config.config_string(SERVERS) in nsi
    assert 'File /oname=$TEMP\\${MSI_FILE} "rustdesk.msi"' in nsi
    assert (
        "rustdesk-1.5.0-aarch64-preconfigured.exe" in nsi
        and "rustdesk-1.5.0-aarch64-preconfigured.exe" in ps1
    )
    assert "https://github.com/rustdesk/rustdesk/releases/download/nightly/rustdesk-1.5.0-aarch64.msi" in ps1
    assert MSI_SHA.upper() in ps1  # PowerShell compares hashes without regard to case; GitHub's is lower-case
    # The nightly has no tag to fetch the icon from.
    assert "/master/res/icon.ico" in ps1
    assert "-Thumbprint" in files["README.txt"] and "signtool" in ps1
    # The kit is not signed and contains no signing command from the server.
    assert "!finalize" not in nsi


def test_the_kit_refuses_an_architecture_the_release_does_not_have():
    stable = inst.parse_releases(_payload())[1]
    with pytest.raises(inst.InstallerError, match="no MSI"):
        inst.build_kit(SERVERS, stable, "arm64")


def test_the_output_names_follow_the_release(tmp_path):
    # The generated names contain only characters that are safe in a file name and a script.
    kit = inst.build_kit(SERVERS, inst.parse_releases(_payload())[1], "x64")
    with zipfile.ZipFile(io.BytesIO(kit)) as archive:
        assert re.search(r"rustdesk-1\.4\.9-x86_64-preconfigured\.exe", archive.read("build.ps1").decode())
