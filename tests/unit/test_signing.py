"""The code-signing certificate: what is checked, how it is kept, how it is used to sign."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from rustdesk_api.config import Settings
from rustdesk_api.security.encryption import generate_key
from rustdesk_api.services import signing

PASSWORD = "typed-by-the-person-1234"
SUBJECT = "Example Corp Code Signing"


def make_pfx(
    password: str = PASSWORD,
    *,
    days: int = 365,
    starts: int = -1,
    usage=(ExtendedKeyUsageOID.CODE_SIGNING,),
    with_key: bool = True,
    key=None,
) -> bytes:
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, SUBJECT),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Example Corp"),
        ]
    )
    now = dt.datetime.now(dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + dt.timedelta(days=starts))
        .not_valid_after(now + dt.timedelta(days=days))
    )
    if usage is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage(list(usage)), critical=False)
    cert = builder.sign(key, None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256())
    return pkcs12.serialize_key_and_certificates(
        b"test",
        key if with_key else None,
        cert,
        None,
        serialization.BestAvailableEncryption(password.encode()),
    )


@pytest.fixture()
def settings() -> Settings:
    return Settings.model_construct().model_copy(
        update={"data_encryption_key": generate_key(), "installer_osslsigncode": sys.executable}
    )


def _store(settings, tmp_path, pfx=None, password=PASSWORD, who="admin"):
    return signing.store(settings, tmp_path, pfx if pfx is not None else make_pfx(), password, who)


# --- what is accepted and refused -------------------------------------------


def test_a_good_certificate_is_kept_and_described(settings, tmp_path):
    stored = _store(settings, tmp_path)
    assert stored.subject == SUBJECT and stored.issuer == SUBJECT
    assert len(stored.thumbprint) == 40 and stored.thumbprint == stored.thumbprint.upper()
    assert stored.uploaded_by == "admin" and not stored.expired
    assert signing.info(tmp_path) == stored


def test_nothing_is_kept_without_the_encryption_key(settings, tmp_path):
    settings = settings.model_copy(update={"data_encryption_key": ""})
    with pytest.raises(signing.SigningError) as caught:
        _store(settings, tmp_path)
    assert caught.value.code == "ENCRYPTION_KEY_REQUIRED" and caught.value.status == 409
    assert signing.info(tmp_path) is None and not (tmp_path / signing.FOLDER).exists()


def _bad(why, pfx, password=PASSWORD):
    # A named case: the bytes of a file must not become the test's id (a huge environment variable).
    return pytest.param(pfx, password, id=why)


@pytest.mark.parametrize(
    ("pfx", "password"),
    [
        _bad("not a pfx", b"this is not a pfx file"),
        _bad("wrong password", make_pfx(), "the-wrong-password"),
        _bad("empty", b""),
        _bad("too big", b"x" * (signing.MAX_PFX_BYTES + 1)),
        _bad("expired", make_pfx(days=-2, starts=-400)),
        _bad("not valid yet", make_pfx(starts=3)),
        _bad("not for code signing", make_pfx(usage=(ExtendedKeyUsageOID.SERVER_AUTH,))),
        _bad("no private key", make_pfx(with_key=False)),
        _bad("a key that cannot sign", make_pfx(key=ed25519.Ed25519PrivateKey.generate())),
    ],
)
def test_a_bad_certificate_is_refused_and_nothing_is_kept(settings, tmp_path, pfx, password):
    with pytest.raises(signing.SigningError) as caught:
        _store(settings, tmp_path, pfx, password)
    assert password not in str(caught.value)
    assert signing.info(tmp_path) is None


def test_no_stated_key_usage_is_accepted(settings, tmp_path):
    assert _store(settings, tmp_path, make_pfx(usage=None)).subject == SUBJECT


def test_any_extended_usage_is_accepted(settings, tmp_path):
    pfx = make_pfx(usage=(ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE,))
    assert _store(settings, tmp_path, pfx).subject == SUBJECT


def test_a_certificate_without_a_password_can_be_uploaded(settings, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "No password")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=10))
        .sign(key, hashes.SHA256())
    )
    plain = pkcs12.serialize_key_and_certificates(b"n", key, cert, None, serialization.NoEncryption())
    assert _store(settings, tmp_path, plain, "").subject == "No password"


# --- how it is kept ------------------------------------------------------------


def test_the_stored_files_hold_no_secret_in_the_clear(settings, tmp_path):
    pfx = make_pfx()
    stored = _store(settings, tmp_path, pfx)
    folder = tmp_path / signing.FOLDER
    assert sorted(p.name for p in folder.iterdir()) == sorted([signing.INFO_FILE, signing.SECRET_FILE])
    sealed = (folder / signing.SECRET_FILE).read_bytes()
    everything = b"".join(p.read_bytes() for p in folder.iterdir())
    # The typed password, the uploaded file, the private key and even the certificate's name are not in it.
    assert PASSWORD.encode() not in everything.replace(stored.thumbprint.encode(), b"")
    assert pfx not in everything and SUBJECT.encode() not in sealed and b"PRIVATE" not in everything
    # The public details are in the description file only.
    described = (folder / signing.INFO_FILE).read_text(encoding="utf-8")
    assert stored.thumbprint in described and "password" not in described.lower()


def test_the_password_the_person_typed_is_not_the_one_kept(settings, tmp_path):
    _store(settings, tmp_path)
    pfx, kept_password = signing._load(settings, tmp_path)
    assert kept_password != PASSWORD
    key, cert, _ = pkcs12.load_key_and_certificates(pfx, kept_password.encode())
    assert key is not None and cert is not None
    with pytest.raises(ValueError):
        pkcs12.load_key_and_certificates(pfx, PASSWORD.encode())


def test_uploading_again_replaces_it_and_removing_deletes_it(settings, tmp_path):
    first = _store(settings, tmp_path)
    second = _store(settings, tmp_path, who="someone-else")
    assert second.thumbprint != first.thumbprint and signing.info(tmp_path) == second
    assert signing.remove(tmp_path) is True
    assert signing.info(tmp_path) is None and list((tmp_path / signing.FOLDER).iterdir()) == []
    assert signing.remove(tmp_path) is False


def test_a_lost_key_is_reported_not_crashed_on(settings, tmp_path):
    _store(settings, tmp_path)
    other = settings.model_copy(update={"data_encryption_key": generate_key()})
    with pytest.raises(signing.SigningError) as caught:
        signing._load(other, tmp_path)
    assert caught.value.code == "CERTIFICATE_UNREADABLE"


def test_rotating_the_key_keeps_the_certificate_usable(settings, tmp_path):
    _store(settings, tmp_path)
    new = generate_key()
    both = settings.model_copy(update={"data_encryption_key": f"{new},{settings.data_encryption_key}"})
    assert signing.rotate(both, tmp_path) == (1, 0)
    only_new = settings.model_copy(update={"data_encryption_key": new})
    assert signing._load(only_new, tmp_path)[0]
    assert signing.rotate(only_new, tmp_path) == (1, 0)
    lost = settings.model_copy(update={"data_encryption_key": generate_key()})
    assert signing.rotate(lost, tmp_path) == (0, 1)


def test_rotating_with_nothing_stored_does_nothing(settings, tmp_path):
    assert signing.rotate(settings, tmp_path) == (0, 0)


def test_a_damaged_description_reads_as_no_certificate(settings, tmp_path):
    _store(settings, tmp_path)
    (tmp_path / signing.FOLDER / signing.INFO_FILE).write_text("{not json", encoding="utf-8")
    assert signing.info(tmp_path) is None


# --- signing --------------------------------------------------------------------


class FakeTool:
    """Stands in for osslsigncode: records what it was given and writes the signed file."""

    def __init__(self, monkeypatch, returncode=0, output=b"", write=True):
        self.calls: list[dict] = []
        self.returncode, self.output, self.write = returncode, output, write
        monkeypatch.setattr(signing, "_run", self)

    def __call__(self, argv, **kwargs):
        opts = {argv[i]: argv[i + 1] for i in range(2, len(argv) - 1) if argv[i].startswith("-")}
        seen = {
            "argv": list(argv),
            "opts": opts,
            "certificate_existed": (signing.Path(opts["-pkcs12"]).is_file() if "-pkcs12" in opts else False),
            "password_file_text": signing.Path(opts["-readpass"]).read_text(encoding="ascii"),
            "scratch": signing.Path(opts["-pkcs12"]).parent,
            "kwargs": kwargs,
        }
        self.calls.append(seen)
        if self.write:
            signing.Path(opts["-out"]).write_bytes(signing.Path(opts["-in"]).read_bytes() + b"<signature>")
        return SimpleNamespace(returncode=self.returncode, stdout=self.output, stderr=b"")


def _installer(tmp_path):
    target = tmp_path / "setup.exe"
    target.write_bytes(b"MZ-installer")
    return target


def test_signing_replaces_the_file_and_leaves_nothing_behind(settings, tmp_path, monkeypatch):
    _store(settings, tmp_path)
    target = _installer(tmp_path)
    tool = FakeTool(monkeypatch)
    signing.sign_file(settings, tmp_path, target)
    (call,) = tool.calls
    assert target.read_bytes() == b"MZ-installer<signature>"
    assert call["argv"][:2] == [sys.executable, "sign"] and call["opts"]["-h"] == "sha256"
    assert call["opts"]["-in"] == str(target) and call["certificate_existed"]
    assert call["opts"]["-ts"] == "http://timestamp.digicert.com"
    assert not call["scratch"].exists(), "the folder that held the key is deleted"
    assert not list(tmp_path.glob("*.signing")) and not list(tmp_path.glob("*.part"))


def test_the_password_goes_in_a_file_never_on_the_command_line(settings, tmp_path, monkeypatch):
    _store(settings, tmp_path)
    tool = FakeTool(monkeypatch)
    signing.sign_file(settings, tmp_path, _installer(tmp_path))
    (call,) = tool.calls
    kept_password = signing._load(settings, tmp_path)[1]
    assert call["password_file_text"] == kept_password
    assert kept_password not in " ".join(call["argv"]) and PASSWORD not in " ".join(call["argv"])
    assert call["kwargs"].get("shell") is None and "input" not in call["kwargs"]


def test_no_timestamp_url_means_no_timestamp_option(settings, tmp_path, monkeypatch):
    _store(settings, tmp_path)
    tool = FakeTool(monkeypatch)
    signing.sign_file(
        settings.model_copy(update={"installer_timestamp_url": ""}), tmp_path, _installer(tmp_path)
    )
    assert "-ts" not in tool.calls[0]["argv"]


def test_a_failed_signing_keeps_the_original_and_says_why_without_secrets(settings, tmp_path, monkeypatch):
    _store(settings, tmp_path)
    target = _installer(tmp_path)
    kept_password = signing._load(settings, tmp_path)[1]
    FakeTool(
        monkeypatch,
        returncode=1,
        output=f"Failed to get a timestamp\nbad password {kept_password}\n".encode(),
        write=False,
    )
    with pytest.raises(signing.SigningError) as caught:
        signing.sign_file(settings, tmp_path, target)
    message = str(caught.value)
    assert "exit code 1" in message and "timestamp" in message
    assert kept_password not in message and PASSWORD not in message
    assert target.read_bytes() == b"MZ-installer" and not list(tmp_path.glob("*.signing"))


def test_a_tool_that_succeeds_without_output_is_a_failure(settings, tmp_path, monkeypatch):
    _store(settings, tmp_path)
    FakeTool(monkeypatch, returncode=0, write=False)
    with pytest.raises(signing.SigningError):
        signing.sign_file(settings, tmp_path, _installer(tmp_path))


def test_a_slow_tool_is_stopped(settings, tmp_path, monkeypatch):
    _store(settings, tmp_path)

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(signing, "_run", slow)
    with pytest.raises(signing.SigningError, match="too long"):
        signing.sign_file(settings, tmp_path, _installer(tmp_path))


def test_a_missing_tool_is_reported(settings, tmp_path):
    _store(settings, tmp_path)
    missing = settings.model_copy(update={"installer_osslsigncode": str(tmp_path / "nope-osslsigncode")})
    assert signing.find_tool(missing) is None
    with pytest.raises(signing.SigningError) as caught:
        signing.sign_file(missing, tmp_path, _installer(tmp_path))
    assert caught.value.code == "SIGNING_TOOL_NOT_FOUND"


def test_signing_without_a_stored_certificate_is_refused(settings, tmp_path):
    with pytest.raises(signing.SigningError) as caught:
        signing.sign_file(settings, tmp_path, _installer(tmp_path))
    assert caught.value.code == "CERTIFICATE_MISSING"


def test_the_timestamp_host_is_only_the_host():
    private = Settings.model_construct().model_copy(
        update={"installer_timestamp_url": "https://user:pw@ts.example.com/path?token=abc"}
    )
    assert signing.timestamp_host(private) == "ts.example.com"
    assert signing.timestamp_host(private.model_copy(update={"installer_timestamp_url": ""})) is None


# --- the real tools, when both are installed ------------------------------------

_NSI = 'OutFile "{out}"\nSection\nSectionEnd\n'


def _real_tool() -> str | None:
    return signing.find_tool(Settings.model_construct())


@pytest.mark.skipif(_real_tool() is None, reason="osslsigncode is not installed")
def test_a_real_installer_is_really_signed(settings, tmp_path):
    from rustdesk_api.services import installer as installer_service

    makensis = installer_service.find_makensis(Settings.model_construct())
    if makensis is None:
        pytest.skip("makensis is not installed")
    script = tmp_path / "t.nsi"
    target = tmp_path / "setup.exe"
    script.write_text(_NSI.format(out=target), encoding="utf-8")
    subprocess.run([makensis, "-V1", str(script)], check=True, capture_output=True)  # noqa: S603
    before = target.read_bytes()
    real = settings.model_copy(update={"installer_osslsigncode": _real_tool(), "installer_timestamp_url": ""})
    _store(real, tmp_path)
    signing.sign_file(real, tmp_path, target)
    after = target.read_bytes()
    assert after != before and len(after) > len(before)
    verified = subprocess.run(  # noqa: S603
        [_real_tool(), "verify", "-in", str(target)], capture_output=True, check=False
    )
    text = (verified.stdout + verified.stderr).decode("utf-8", errors="replace")
    # Self-signed, so the chain is untrusted; what matters is that the signature is there and the
    # digest matches.
    assert "Message digest algorithm  : SHA256" in text
    current = [line for line in text.splitlines() if line.startswith("Current message digest")]
    calculated = [line for line in text.splitlines() if line.startswith("Calculated message digest")]
    assert current and calculated
    assert current[0].split(":", 1)[1].strip() == calculated[0].split(":", 1)[1].strip()
