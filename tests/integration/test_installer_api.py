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
    assert (body["available"], body["reason"], body["signing"], body["certificate"]) == (
        True,
        None,
        False,
        None,
    )
    # Whether osslsigncode is installed depends on the machine, so only its presence in the answer is checked.
    assert isinstance(body["certificate_tool"], bool) and body["certificate_storage"] is False


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


# --- the uploaded code-signing certificate ------------------------------------------------

CERT_PASSWORD = "the-password-typed-in-the-form"


def _pfx(password=CERT_PASSWORD, days=365, starts=-1) -> bytes:
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Example Corp Code Signing")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + dt.timedelta(days=starts))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(
        b"t", key, cert, None, serialization.BestAvailableEncryption(password.encode())
    )


def _upload(client, pfx=None, password=CERT_PASSWORD):
    import base64

    return client.put(
        "/api/v1/admin/installer/certificate",
        json={
            "pfx_base64": base64.b64encode(pfx if pfx is not None else _pfx()).decode(),
            "password": password,
        },
    )


class FakeSigner:
    """Stands in for osslsigncode: appends a marker to the file it is asked to sign."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.fail = False

    def __call__(self, argv, **kwargs):
        from types import SimpleNamespace

        from rustdesk_api.services import signing

        self.calls.append(list(argv))
        opts = {argv[i]: argv[i + 1] for i in range(2, len(argv) - 1) if argv[i].startswith("-")}
        if self.fail:
            return SimpleNamespace(returncode=1, stdout=b"Failed to reach the timestamp server\n", stderr=b"")
        signing.Path(opts["-out"]).write_bytes(signing.Path(opts["-in"]).read_bytes() + b"<signed>")
        return SimpleNamespace(returncode=0, stdout=b"Succeeded", stderr=b"")


@pytest.fixture()
def certificate_env(installer_env, monkeypatch):
    """The installer environment plus a data key, a signing tool that exists and a fake that signs."""
    from rustdesk_api.security.encryption import generate_key
    from rustdesk_api.services import signing

    monkeypatch.setenv("DATA_ENCRYPTION_KEY", generate_key())
    monkeypatch.setenv("INSTALLER_OSSLSIGNCODE", sys.executable)  # any existing file: it is never run
    signer = FakeSigner()
    monkeypatch.setattr(signing, "_run", signer)
    return signer


def test_a_certificate_can_be_uploaded_and_is_described_without_any_secret(certificate_env, admin_client):
    r = _upload(admin_client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subject"] == "Example Corp Code Signing" and body["uploaded_by"] == "admin"
    assert len(body["thumbprint"]) == 40 and body["expired"] is False and body["code_signing"] is True
    status = admin_client.get("/api/v1/admin/installer").json()
    assert status["certificate"]["thumbprint"] == body["thumbprint"]
    assert status["certificate_tool"] is True and status["certificate_storage"] is True
    assert status["timestamp_host"] == "timestamp.digicert.com"
    for text in (r.text, str(status)):
        assert CERT_PASSWORD not in text and "PRIVATE" not in text and "pfx" not in text.lower()


def test_the_status_without_a_certificate_or_a_data_key(installer_env, admin_client):
    status = admin_client.get("/api/v1/admin/installer").json()
    assert status["certificate"] is None and status["certificate_storage"] is False


def test_a_certificate_needs_the_data_key(installer_env, admin_client):
    r = _upload(admin_client)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ENCRYPTION_KEY_REQUIRED"


def test_a_wrong_password_is_refused_without_repeating_it(certificate_env, admin_client):
    r = _upload(admin_client, password="wrong-wrong-wrong")
    assert r.status_code == 400 and r.json()["error"]["code"] == "SIGNING_ERROR"
    assert "wrong-wrong-wrong" not in r.text
    assert admin_client.get("/api/v1/admin/installer").json()["certificate"] is None


def test_an_expired_certificate_is_refused(certificate_env, admin_client):
    r = _upload(admin_client, pfx=_pfx(days=-2, starts=-400))
    assert r.status_code == 400 and "expired" in r.json()["error"]["message"]


def test_a_badly_sent_file_is_refused_and_what_was_sent_is_not_echoed(certificate_env, admin_client):
    r = admin_client.put(
        "/api/v1/admin/installer/certificate", json={"pfx_base64": "%%% not base64 %%%", "password": "p"}
    )
    assert r.status_code == 422
    too_long = "long-secret-" * 40
    r = admin_client.put(
        "/api/v1/admin/installer/certificate", json={"pfx_base64": "AAAA", "password": too_long}
    )
    assert r.status_code == 422 and "long-secret-" not in r.text


def test_only_administrators_can_manage_the_certificate_and_it_needs_csrf(certificate_env, app, admin_client):
    bob = _ordinary_user(app, admin_client)
    assert _upload(bob).status_code == 403
    assert bob.delete("/api/v1/admin/installer/certificate").status_code == 403
    assert _upload(TestClient(app)).status_code == 401
    no_csrf = TestClient(app)
    no_csrf.cookies.update(admin_client.cookies)
    assert _upload(no_csrf).status_code == 403
    assert no_csrf.delete("/api/v1/admin/installer/certificate").status_code == 403


def test_a_build_is_signed_with_the_uploaded_certificate(certificate_env, admin_client):
    _upload(admin_client)
    started = admin_client.post("/api/v1/admin/installer/builds", json=_body())
    job = _wait(admin_client, started.json()["id"])
    assert job["state"] == "done" and job["signed"] is True and job["warnings"] == []
    assert len(certificate_env.calls) == 1 and "-ts" in certificate_env.calls[0]
    downloaded = admin_client.get(f"/api/v1/admin/installer/builds/{job['id']}/file")
    assert downloaded.content.endswith(b"<signed>")
    assert job["size"] == len(downloaded.content)
    # The password and the key never appear on the command line.
    assert CERT_PASSWORD not in " ".join(certificate_env.calls[0])


def test_unticking_the_box_builds_an_unsigned_installer(certificate_env, admin_client):
    _upload(admin_client)
    started = admin_client.post("/api/v1/admin/installer/builds", json=_body(sign=False))
    job = _wait(admin_client, started.json()["id"])
    assert job["state"] == "done" and job["signed"] is False and certificate_env.calls == []


def test_asking_to_sign_without_a_certificate_is_refused_at_once(certificate_env, admin_client):
    r = admin_client.post("/api/v1/admin/installer/builds", json=_body(sign=True))
    assert r.status_code == 409 and r.json()["error"]["code"] == "CERTIFICATE_MISSING"


def test_without_a_certificate_a_build_is_simply_unsigned(certificate_env, admin_client):
    job = _wait(admin_client, admin_client.post("/api/v1/admin/installer/builds", json=_body()).json()["id"])
    assert job["state"] == "done" and job["signed"] is False and job["warnings"] == []


def test_the_command_of_the_server_wins_over_the_certificate(certificate_env, signing_env, admin_client):
    _upload(admin_client)
    job = _wait(admin_client, admin_client.post("/api/v1/admin/installer/builds", json=_body()).json()["id"])
    assert job["state"] == "done" and job["signed"] is True
    assert certificate_env.calls == [], "the uploaded certificate was not used as well"


def test_a_missing_tool_leaves_the_build_unsigned_unless_signing_was_demanded(
    certificate_env, monkeypatch, admin_client, tmp_path
):
    _upload(admin_client)
    monkeypatch.setenv("INSTALLER_OSSLSIGNCODE", str(tmp_path / "no-such-tool"))
    from rustdesk_api.config import clear_settings_cache

    clear_settings_cache()
    status = admin_client.get("/api/v1/admin/installer").json()
    assert status["certificate_tool"] is False
    job = _wait(admin_client, admin_client.post("/api/v1/admin/installer/builds", json=_body()).json()["id"])
    assert job["state"] == "done" and job["signed"] is False and job["warnings"] == ["signing_tool_missing"]
    demanded = admin_client.post("/api/v1/admin/installer/builds", json=_body(sign=True))
    assert demanded.status_code == 409 and demanded.json()["error"]["code"] == "SIGNING_TOOL_NOT_FOUND"


def test_a_failed_signing_fails_the_build_and_leaves_no_unsigned_file(
    certificate_env, admin_client, tmp_path
):
    _upload(admin_client)
    certificate_env.fail = True
    job = _wait(admin_client, admin_client.post("/api/v1/admin/installer/builds", json=_body()).json()["id"])
    assert job["state"] == "failed" and job["message"].startswith("Signing failed:")
    assert "timestamp" in job["message"] and CERT_PASSWORD not in job["message"]
    assert admin_client.get(f"/api/v1/admin/installer/builds/{job['id']}/file").status_code == 409
    assert list((tmp_path / "installers" / "out").glob("*.exe")) == []


def test_the_certificate_can_be_removed_and_replaced(certificate_env, admin_client):
    first = _upload(admin_client).json()
    second = _upload(admin_client).json()
    assert second["thumbprint"] != first["thumbprint"]
    r = admin_client.delete("/api/v1/admin/installer/certificate")
    assert r.status_code == 200 and r.json() == {"deleted": 1}
    assert admin_client.get("/api/v1/admin/installer").json()["certificate"] is None
    assert admin_client.delete("/api/v1/admin/installer/certificate").json() == {"deleted": 0}


def test_the_certificate_cannot_change_while_a_build_runs(certificate_env, monkeypatch, admin_client):
    _upload(admin_client)
    started, release = threading.Event(), threading.Event()

    def slow(makensis, script, sign_command=""):
        started.set()
        release.wait(5)
        return "ok"

    monkeypatch.setattr(inst, "run_makensis", slow)
    first = admin_client.post("/api/v1/admin/installer/builds", json=_body(sign=False))
    assert started.wait(5)
    try:
        assert _upload(admin_client).status_code == 409
        assert admin_client.delete("/api/v1/admin/installer/certificate").status_code == 409
    finally:
        release.set()
        _wait(admin_client, first.json()["id"])


def test_certificate_changes_and_signed_builds_are_audited_without_secrets(certificate_env, admin_client):
    uploaded = _upload(admin_client).json()
    _wait(admin_client, admin_client.post("/api/v1/admin/installer/builds", json=_body()).json()["id"])
    admin_client.delete("/api/v1/admin/installer/certificate")
    logs = admin_client.get("/api/v1/admin/audit-logs?page_size=50")
    actions = {e["action"]: e for e in logs.json()["items"]}
    assert actions["installer_certificate_set"]["detail"]["thumbprint"] == uploaded["thumbprint"]
    assert actions["installer_certificate_removed"]["detail"]["thumbprint"] == uploaded["thumbprint"]
    assert actions["installer_build"]["detail"]["certificate"] is True
    assert CERT_PASSWORD not in logs.text and "pfx" not in logs.text.lower()


def test_rotating_the_data_key_keeps_the_certificate_usable(certificate_env, monkeypatch, admin_client):
    import os

    from click.testing import CliRunner

    from rustdesk_api.cli import main
    from rustdesk_api.config import clear_settings_cache
    from rustdesk_api.security.encryption import generate_key

    _upload(admin_client)
    old_key = os.environ["DATA_ENCRYPTION_KEY"]
    new_key = generate_key()
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", f"{new_key},{old_key}")
    clear_settings_cache()
    result = CliRunner().invoke(main, ["rotate-data-key"])
    assert result.exit_code == 0, result.output
    assert "1 signing certificate" in result.output
    # With only the new key left the certificate still opens: sign something with it.
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", new_key)
    clear_settings_cache()
    from rustdesk_api.config import get_settings
    from rustdesk_api.services import signing

    target = signing.Path(get_settings().installer_dir) / "probe.exe"
    target.write_bytes(b"MZ")
    signing.sign_file(get_settings(), signing.Path(get_settings().installer_dir), target)
    assert target.read_bytes() == b"MZ<signed>"
