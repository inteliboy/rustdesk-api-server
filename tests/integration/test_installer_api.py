"""The installer builder over HTTP: administrators only, CSRF, a background build, the kit."""

from __future__ import annotations

import hashlib
import io
import re
import sys
import threading
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from rustdesk_api.services import client_config
from rustdesk_api.services import installer as inst

MSI = b"fake-msi-bytes" * 100
SHA = hashlib.sha256(MSI).hexdigest()
SERVERS = {
    "id_server": "id.example.com",
    "relay_server": "",
    "api_server": "https://api.example.com",
    "key": "pubkey=",
}


def _release(tag, version, arches, prerelease=False):
    names = {"x64": f"rustdesk-{version}-x86_64.msi", "arm64": f"rustdesk-{version}-aarch64.msi"}
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": False,
        "published_at": "2026-07-06T10:02:30Z",
        "assets": [
            {
                "name": names[a],
                "size": len(MSI),
                "digest": f"sha256:{SHA}",
                "browser_download_url": f"https://github.com/rustdesk/rustdesk/releases/download/{tag}/{names[a]}",
            }
            for a in arches
        ],
    }


@pytest.fixture()
def installer_env(monkeypatch, tmp_path):
    """A GitHub that answers from memory and a makensis that only writes the file the script
    names. Requested before `admin_client` so the settings pick the environment up."""
    releases = [
        _release("nightly", "1.5.0", ["x64", "arm64"], prerelease=True),
        _release("1.4.9", "1.4.9", ["x64", "arm64"]),
        _release("1.4.7", "1.4.7", ["x64"]),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.github.com":
            return httpx.Response(200, json=releases)
        if host == "github.com":
            return httpx.Response(302, headers={"location": "https://release-assets.githubusercontent.com/x"})
        if host == "release-assets.githubusercontent.com":
            return httpx.Response(200, content=MSI)
        if host == "raw.githubusercontent.com":
            return httpx.Response(200, content=b"\x00\x00\x01\x00" + b"\x00" * 40)
        return httpx.Response(404)

    scripts: list[str] = []

    def fake_run(makensis, script, sign_command=""):
        text = script.read_text(encoding="utf-8")
        scripts.append(text)
        outfile = re.search(r'^Outfile "(.+)"$', text, re.MULTILINE)[1]
        with open(outfile, "wb") as out:
            out.write(b"MZ-not-really-an-installer")
        return "Total size: 1"

    monkeypatch.setenv("INSTALLER_MAKENSIS", sys.executable)  # any existing file: it is never run
    monkeypatch.setenv("INSTALLER_DIR", str(tmp_path / "installers"))
    monkeypatch.setattr(inst, "run_makensis", fake_run)
    inst.set_transport(httpx.MockTransport(handler))
    yield scripts
    inst.set_transport(None)
    inst.reset_state()


def _ordinary_user(app, admin_client, name="bob"):
    r = admin_client.post("/api/v1/users", json={"username": name, "password": "userpassword1"})
    assert r.status_code == 201, r.text
    other = TestClient(app)
    other.post("/api/v1/auth/login", json={"username": name, "password": "userpassword1"})
    other.headers.update({"X-CSRF-Token": other.cookies.get("rd_csrf")})
    return other


def _wait(client, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/admin/installer/builds/{job_id}").json()
        if job["state"] in ("done", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("the build did not finish")


def _body(**changes):
    body = {"servers": SERVERS, "tag": "1.4.9", "arch": "x64", "reset_settings": False}
    body.update(changes)
    return body


# --- who may ----------------------------------------------------------------


def test_only_administrators_can_use_it(installer_env, app, admin_client):
    bob = _ordinary_user(app, admin_client)
    anonymous = TestClient(app)
    for who, expected in ((bob, 403), (anonymous, 401)):
        assert who.get("/api/v1/admin/installer").status_code == expected
        assert who.get("/api/v1/admin/installer/releases").status_code == expected
        assert who.post("/api/v1/admin/installer/builds", json=_body()).status_code == expected
        assert who.post("/api/v1/admin/installer/kit", json=_body()).status_code == expected
        assert who.get("/api/v1/admin/installer/builds/anything").status_code == expected
        assert who.get("/api/v1/admin/installer/builds/anything/file").status_code == expected


def test_building_needs_the_csrf_token(installer_env, admin_client):
    admin_client.headers.pop("X-CSRF-Token")
    assert admin_client.post("/api/v1/admin/installer/builds", json=_body()).status_code == 403
    assert admin_client.post("/api/v1/admin/installer/kit", json=_body()).status_code == 403


# --- status and releases ----------------------------------------------------


def test_the_status_says_whether_the_server_can_build_and_sign(installer_env, admin_client):
    body = admin_client.get("/api/v1/admin/installer").json()
    assert body == {"available": True, "reason": None, "signing": False}


def test_the_status_without_makensis(installer_env, monkeypatch, admin_client):
    monkeypatch.setattr(inst, "find_makensis", lambda settings: None)
    body = admin_client.get("/api/v1/admin/installer").json()
    assert body["available"] is False and body["reason"] == "no_makensis"
    r = admin_client.post("/api/v1/admin/installer/builds", json=_body())
    assert r.status_code == 409 and r.json()["error"]["code"] == "NSIS_NOT_FOUND"
    # The kit needs nothing on the server.
    assert admin_client.post("/api/v1/admin/installer/kit", json=_body()).status_code == 200


def test_releases_list_stable_and_nightly_with_their_architectures(installer_env, admin_client):
    r = admin_client.get("/api/v1/admin/installer/releases")

    assert r.status_code == 200, r.text
    items = {i["tag"]: i for i in r.json()}
    assert list(items) == ["nightly", "1.4.9", "1.4.7"]
    assert items["nightly"]["prerelease"] is True and items["nightly"]["architectures"] == ["arm64", "x64"]
    assert items["1.4.7"]["architectures"] == ["x64"]


def test_a_dead_github_is_a_clear_error(installer_env, admin_client):
    inst.set_transport(httpx.MockTransport(lambda request: httpx.Response(503)))
    r = admin_client.get("/api/v1/admin/installer/releases")
    assert r.status_code == 502 and r.json()["error"]["code"] == "INSTALLER_FAILED"


# --- building -----------------------------------------------------------------


def test_a_build_runs_in_the_background_and_the_file_can_be_downloaded(installer_env, admin_client):
    r = admin_client.post("/api/v1/admin/installer/builds", json=_body())
    assert r.status_code == 202, r.text
    job = _wait(admin_client, r.json()["id"])

    assert job["state"] == "done", job
    assert job["filename"] == "rustdesk-1.4.9-x86_64-preconfigured.exe"
    assert job["size"] == len(b"MZ-not-really-an-installer") and job["signed"] is False
    file = admin_client.get(f"/api/v1/admin/installer/builds/{job['id']}/file")
    assert file.status_code == 200 and file.content == b"MZ-not-really-an-installer"
    assert "rustdesk-1.4.9-x86_64-preconfigured.exe" in file.headers["content-disposition"]
    assert file.headers["cache-control"] == "no-store"
    # The script that was built holds the servers from the request.
    config = client_config.config_string(
        client_config.ClientServers("id.example.com", "", "https://api.example.com", "pubkey=")
    )
    assert f"--config {config}" in installer_env[0]
    assert "RMDir" not in installer_env[0]


def test_the_reset_option_and_the_architecture_reach_the_script(installer_env, admin_client):
    r = admin_client.post(
        "/api/v1/admin/installer/builds", json=_body(tag="nightly", arch="arm64", reset_settings=True)
    )
    job = _wait(admin_client, r.json()["id"])

    assert job["state"] == "done" and job["filename"] == "rustdesk-1.5.0-aarch64-preconfigured.exe"
    assert "RMDir" in installer_env[0] and "(arm64)" in installer_env[0]


@pytest.fixture()
def signing_env(monkeypatch):
    monkeypatch.setenv("INSTALLER_SIGN_COMMAND", 'signtool sign /sha1 SECRETTHUMB "%1"')


def test_signing_is_reported_when_a_command_is_configured(installer_env, signing_env, admin_client):
    assert admin_client.get("/api/v1/admin/installer").json()["signing"] is True
    job = _wait(admin_client, admin_client.post("/api/v1/admin/installer/builds", json=_body()).json()["id"])
    assert job["state"] == "done" and job["signed"] is True
    # The command reaches the script, but never the answers of the API.
    assert "SECRETTHUMB" in installer_env[0]
    assert (
        "SECRETTHUMB" not in str(job)
        and "SECRETTHUMB" not in admin_client.get("/api/v1/admin/installer").text
    )


def test_an_unknown_release_or_missing_architecture_is_refused_at_once(installer_env, admin_client):
    r = admin_client.post("/api/v1/admin/installer/builds", json=_body(tag="9.9.9"))
    assert r.status_code == 404 and r.json()["error"]["code"] == "RELEASE_NOT_FOUND"
    r = admin_client.post("/api/v1/admin/installer/builds", json=_body(tag="1.4.7", arch="arm64"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "ARCH_NOT_AVAILABLE"
    assert admin_client.post("/api/v1/admin/installer/builds", json=_body(arch="mips")).status_code == 422
    assert (
        admin_client.post(
            "/api/v1/admin/installer/builds", json=_body(servers={**SERVERS, "id_server": ""})
        ).status_code
        == 422
    )


def test_a_failed_build_says_why_and_offers_no_file(installer_env, monkeypatch, admin_client):
    def failing(makensis, script, sign_command=""):
        raise inst.InstallerError("makensis failed (exit code 1).\nError: nope")

    monkeypatch.setattr(inst, "run_makensis", failing)
    job = _wait(admin_client, admin_client.post("/api/v1/admin/installer/builds", json=_body()).json()["id"])

    assert job["state"] == "failed" and "nope" in job["message"]
    assert admin_client.get(f"/api/v1/admin/installer/builds/{job['id']}/file").status_code == 409


def test_a_second_build_while_one_runs_is_refused(installer_env, monkeypatch, admin_client):
    started, release = threading.Event(), threading.Event()

    def slow(makensis, script, sign_command=""):
        started.set()
        release.wait(5)
        return "ok"

    monkeypatch.setattr(inst, "run_makensis", slow)
    first = admin_client.post("/api/v1/admin/installer/builds", json=_body())
    assert first.status_code == 202
    assert started.wait(5)
    second = admin_client.post("/api/v1/admin/installer/builds", json=_body())
    release.set()

    assert second.status_code == 409 and second.json()["error"]["code"] == "INSTALLER_BUSY"
    _wait(admin_client, first.json()["id"])
    # And once it is over a new one is accepted.
    assert admin_client.post("/api/v1/admin/installer/builds", json=_body()).status_code == 202


def test_an_unknown_build_is_not_found(installer_env, admin_client):
    assert admin_client.get("/api/v1/admin/installer/builds/nope").status_code == 404
    assert admin_client.get("/api/v1/admin/installer/builds/nope/file").status_code == 404


def test_building_is_recorded_in_the_audit_log_without_the_servers(installer_env, admin_client):
    _wait(admin_client, admin_client.post("/api/v1/admin/installer/builds", json=_body()).json()["id"])
    logs = admin_client.get("/api/v1/admin/audit-logs").json()
    entries = [e for e in logs["items"] if e["action"] == "installer_build"]

    assert entries and "1.4.9" in str(entries[0])
    assert "pubkey" not in str(entries[0]) and "id.example.com" not in str(entries[0])


# --- the kit --------------------------------------------------------------------


def test_the_kit_downloads_as_a_zip(installer_env, admin_client):
    r = admin_client.post("/api/v1/admin/installer/kit", json=_body(tag="nightly", arch="arm64"))

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert "rustdesk-1.5.0-arm64-kit.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        assert sorted(archive.namelist()) == ["README.txt", "build.ps1", "rustdesk.nsi"]
    assert (
        admin_client.post("/api/v1/admin/installer/kit", json=_body(tag="1.4.7", arch="arm64")).status_code
        == 409
    )
    assert admin_client.post("/api/v1/admin/installer/kit", json=_body(tag="nope")).status_code == 404


# --- stored files -----------------------------------------------------------------


def _built(admin_client):
    return _wait(admin_client, admin_client.post("/api/v1/admin/installer/builds", json=_body()).json()["id"])


def test_the_stored_files_are_listed_with_where_they_are(installer_env, admin_client, tmp_path):
    _built(admin_client)
    r = admin_client.get("/api/v1/admin/installer/files")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["directory"] == str(tmp_path / "installers")
    kinds = {i["kind"]: i for i in body["items"]}
    assert set(kinds) == {"msi", "installer"}
    assert kinds["msi"]["name"].endswith("rustdesk-1.4.9-x86_64.msi") and kinds["msi"]["size"] == len(MSI)
    assert re.fullmatch(r"rustdesk-1\.4\.9-x86_64-[A-Za-z0-9_-]{10}\.exe", kinds["installer"]["name"])


def test_a_stored_installer_can_be_downloaded_again_but_an_msi_is_not_served(installer_env, admin_client):
    _built(admin_client)
    items = admin_client.get("/api/v1/admin/installer/files").json()["items"]
    exe = next(i for i in items if i["kind"] == "installer")
    msi = next(i for i in items if i["kind"] == "msi")

    r = admin_client.get(f"/api/v1/admin/installer/files/installer/{exe['name']}")
    assert r.status_code == 200 and r.content == b"MZ-not-really-an-installer"
    assert admin_client.get(f"/api/v1/admin/installer/files/msi/{msi['name']}").status_code in (404, 405)


def test_one_file_or_a_whole_kind_can_be_deleted(installer_env, admin_client):
    _built(admin_client)
    items = admin_client.get("/api/v1/admin/installer/files").json()["items"]
    msi = next(i for i in items if i["kind"] == "msi")

    r = admin_client.delete(f"/api/v1/admin/installer/files/msi/{msi['name']}")
    assert r.status_code == 200 and r.json() == {"deleted": 1}
    assert [i["kind"] for i in admin_client.get("/api/v1/admin/installer/files").json()["items"]] == [
        "installer"
    ]
    assert admin_client.delete(f"/api/v1/admin/installer/files/msi/{msi['name']}").status_code == 404

    r = admin_client.delete("/api/v1/admin/installer/files/installer")
    assert r.json() == {"deleted": 1}
    assert admin_client.get("/api/v1/admin/installer/files").json()["items"] == []
    # A deleted build is not offered any more, rather than failing half way through a download.
    assert admin_client.delete("/api/v1/admin/installer/files/installer").json() == {"deleted": 0}


def test_deleting_cannot_reach_outside_the_folder_or_other_kinds_of_file(
    installer_env, admin_client, tmp_path
):
    _built(admin_client)
    (tmp_path / "installers" / "msi" / "notes.txt").write_text("keep me")
    (tmp_path / "secret.msi").write_bytes(b"outside")
    for name in (
        "notes.txt",
        "..%2Fsecret.msi",
        "..%5Csecret.msi",
        "%2e%2e",
        ".hidden.msi",
        "a" * 300 + ".msi",
    ):
        r = admin_client.delete(f"/api/v1/admin/installer/files/msi/{name}")
        assert r.status_code in (404, 405), (name, r.status_code)
    assert admin_client.delete("/api/v1/admin/installer/files/passwords").status_code == 422
    assert (tmp_path / "installers" / "msi" / "notes.txt").exists() and (tmp_path / "secret.msi").exists()
    # "Delete all" only touches the files of that kind.
    admin_client.delete("/api/v1/admin/installer/files/msi")
    assert (tmp_path / "installers" / "msi" / "notes.txt").exists()


def test_files_are_only_for_administrators_and_deleting_needs_csrf(installer_env, app, admin_client):
    _built(admin_client)
    name = admin_client.get("/api/v1/admin/installer/files").json()["items"][0]["name"]
    bob = _ordinary_user(app, admin_client)
    for who, expected in ((bob, 403), (TestClient(app), 401)):
        assert who.get("/api/v1/admin/installer/files").status_code == expected
        assert who.get(f"/api/v1/admin/installer/files/installer/{name}").status_code == expected
        assert who.delete("/api/v1/admin/installer/files/msi").status_code == expected
    csrf = admin_client.headers.pop("X-CSRF-Token")
    assert admin_client.delete("/api/v1/admin/installer/files/msi").status_code == 403
    admin_client.headers["X-CSRF-Token"] = csrf


def test_nothing_is_deleted_while_a_build_runs(installer_env, monkeypatch, admin_client):
    started, release = threading.Event(), threading.Event()

    def slow(makensis, script, sign_command=""):
        started.set()
        release.wait(5)
        return "ok"

    monkeypatch.setattr(inst, "run_makensis", slow)
    first = admin_client.post("/api/v1/admin/installer/builds", json=_body())
    assert started.wait(5)
    r = admin_client.delete("/api/v1/admin/installer/files/msi")
    release.set()
    _wait(admin_client, first.json()["id"])

    assert r.status_code == 409 and r.json()["error"]["code"] == "INSTALLER_BUSY"


def test_deleting_is_recorded_in_the_audit_log(installer_env, admin_client):
    _built(admin_client)
    admin_client.delete("/api/v1/admin/installer/files/msi")
    logs = admin_client.get("/api/v1/admin/audit-logs").json()["items"]
    assert any(e["action"] == "installer_files_deleted" for e in logs)
