"""The code-signing certificate for the installers this server builds.

An administrator uploads a PKCS#12 file (.pfx / .p12) and its password on the Deploy page.
The server opens it once to check it, then keeps only what it needs to sign:

* the certificate and private key, re-packed under a random password of our own, so the
  password the person typed is never stored;
* both of those encrypted with DATA_ENCRYPTION_KEY (the same box the shared address book's
  passwords use). Without that key nothing is stored: the private key never lands on disk
  in the clear.

It is write-only. Nothing returns the key, the file or a password; the WebUI shows who the
certificate was issued to, by whom, its thumbprint and when it expires. Anyone with an
administrator account can build a signed installer with it, which is the point, and the
reason the upload, the removal and every build that signs are in the audit log.

Signing runs osslsigncode (the Docker image has it; on Windows install it or point
INSTALLER_OSSLSIGNCODE at it). The key is handed to it through a private temporary folder
that is deleted straight after, with the password in a file (`-readpass`), never on the
command line where other processes could read it.
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from rustdesk_api.config import Settings
from rustdesk_api.security.encryption import SecretBox, get_secret_box

logger = logging.getLogger(__name__)

FOLDER = "signing"
SECRET_FILE = "certificate.enc"
INFO_FILE = "certificate.json"
MAX_PFX_BYTES = 256 * 1024
SIGN_TIMEOUT_SECONDS = 180
OUTPUT_TAIL_LINES = 8

# The one place a program is started (tests replace it).
_run = subprocess.run


class SigningError(Exception):
    def __init__(self, message: str, code: str = "SIGNING_ERROR", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class CertificateInfo:
    """What may be shown about the stored certificate. Nothing in here is secret."""

    subject: str
    issuer: str
    thumbprint: str  # SHA-1, upper-case hex, as Windows shows it
    not_before: datetime.datetime
    not_after: datetime.datetime
    uploaded_by: str
    uploaded_at: datetime.datetime
    code_signing: bool  # has the code-signing extended key usage

    @property
    def expired(self) -> bool:
        return self.not_after <= datetime.datetime.now(datetime.timezone.utc)


def _folder(base: Path) -> Path:
    return base / FOLDER


def _box(settings: Settings) -> SecretBox:
    box = get_secret_box(settings.data_encryption_key)
    if box is None:
        raise SigningError(
            "Set DATA_ENCRYPTION_KEY first (rustdesk-api generate-key): the certificate's private key "
            "is stored encrypted with it and is never kept in the clear.",
            "ENCRYPTION_KEY_REQUIRED",
            409,
        )
    return box


def _name(name: x509.Name) -> str:
    for oid in (NameOID.COMMON_NAME, NameOID.ORGANIZATION_NAME):
        found = name.get_attributes_for_oid(oid)
        if found:
            return str(found[0].value)
    return name.rfc4514_string() or "?"


def _write_private(path: Path, data: bytes) -> None:
    """A new file only its owner can read (on Windows the folder's permissions apply)."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def _replace(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".part")
    _write_private(temporary, data)
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# Storing and reading
# ---------------------------------------------------------------------------


def _open_pkcs12(pfx: bytes, password: str):
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(pfx, password.encode("utf-8") or None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        # Never say which: a wrong password and a damaged file look the same, and neither
        # message may repeat what was typed.
        raise SigningError(
            "The certificate could not be opened: the password is wrong or the file is not a "
            "PKCS#12 (.pfx / .p12) file."
        ) from None
    if key is None or cert is None:
        raise SigningError("The file has to hold a certificate together with its private key.")
    if not isinstance(key, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey):
        raise SigningError("Only RSA and elliptic-curve keys can sign an installer.")
    return key, cert, extra


def _code_signing(cert: x509.Certificate) -> bool:
    try:
        usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        return True  # no restriction stated
    return ExtendedKeyUsageOID.CODE_SIGNING in usage or ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE in usage


def store(settings: Settings, base: Path, pfx: bytes, password: str, uploaded_by: str) -> CertificateInfo:
    """Checks the file and keeps it. Raises SigningError with a message fit for the person."""
    box = _box(settings)
    if not pfx or len(pfx) > MAX_PFX_BYTES:
        raise SigningError("That is not a certificate file (too big or empty).")
    key, cert, extra = _open_pkcs12(pfx, password)
    now = datetime.datetime.now(datetime.timezone.utc)
    if cert.not_valid_after_utc <= now:
        raise SigningError("That certificate has expired.")
    if cert.not_valid_before_utc > now:
        raise SigningError("That certificate is not valid yet.")
    if not _code_signing(cert):
        raise SigningError("That certificate is not meant for code signing.")

    own_password = secrets.token_urlsafe(24)
    repacked = pkcs12.serialize_key_and_certificates(
        b"rustdesk-api",
        key,
        cert,
        extra or None,
        serialization.BestAvailableEncryption(own_password.encode("ascii")),
    )
    sealed = box.encrypt(
        json.dumps({"pfx": base64.b64encode(repacked).decode("ascii"), "password": own_password})
    )
    info = CertificateInfo(
        subject=_name(cert.subject),
        issuer=_name(cert.issuer),
        thumbprint=cert.fingerprint(hashes.SHA1()).hex().upper(),  # noqa: S303 - a Windows-style identifier, not security
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        uploaded_by=uploaded_by,
        uploaded_at=now,
        code_signing=True,
    )
    folder = _folder(base)
    folder.mkdir(parents=True, exist_ok=True)
    _replace(folder / SECRET_FILE, sealed.encode("ascii"))
    try:
        _replace(folder / INFO_FILE, json.dumps(_info_json(info)).encode("utf-8"))
    except OSError:
        (folder / SECRET_FILE).unlink(missing_ok=True)
        raise
    return info


def _info_json(info: CertificateInfo) -> dict[str, object]:
    return {
        "subject": info.subject,
        "issuer": info.issuer,
        "thumbprint": info.thumbprint,
        "not_before": info.not_before.isoformat(),
        "not_after": info.not_after.isoformat(),
        "uploaded_by": info.uploaded_by,
        "uploaded_at": info.uploaded_at.isoformat(),
        "code_signing": info.code_signing,
    }


def info(base: Path) -> CertificateInfo | None:
    """The stored certificate's public details, or None when there is none."""
    folder = _folder(base)
    if not (folder / SECRET_FILE).is_file():
        return None
    try:
        raw = json.loads((folder / INFO_FILE).read_text(encoding="utf-8"))
        return CertificateInfo(
            subject=str(raw["subject"]),
            issuer=str(raw["issuer"]),
            thumbprint=str(raw["thumbprint"]),
            not_before=datetime.datetime.fromisoformat(raw["not_before"]),
            not_after=datetime.datetime.fromisoformat(raw["not_after"]),
            uploaded_by=str(raw["uploaded_by"]),
            uploaded_at=datetime.datetime.fromisoformat(raw["uploaded_at"]),
            code_signing=bool(raw.get("code_signing", True)),
        )
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("The stored signing certificate's details could not be read")
        return None


def remove(base: Path) -> bool:
    """Deletes the certificate; True if there was one."""
    folder = _folder(base)
    existed = (folder / SECRET_FILE).is_file()
    for name in (SECRET_FILE, INFO_FILE):
        try:
            (folder / name).unlink(missing_ok=True)
        except OSError as exc:
            raise SigningError(
                f"The certificate could not be removed ({type(exc).__name__}).", "DELETE_FAILED", 500
            ) from exc
    return existed


def _load(settings: Settings, base: Path) -> tuple[bytes, str]:
    box = _box(settings)
    try:
        sealed = (_folder(base) / SECRET_FILE).read_text(encoding="ascii")
    except OSError:
        raise SigningError("There is no certificate stored.", "CERTIFICATE_MISSING", 409) from None
    opened = box.decrypt(sealed)
    if opened is None:
        raise SigningError(
            "The stored certificate cannot be opened with the current DATA_ENCRYPTION_KEY. Upload it again.",
            "CERTIFICATE_UNREADABLE",
            409,
        )
    try:
        data = json.loads(opened)
        return base64.b64decode(data["pfx"]), str(data["password"])
    except (ValueError, KeyError, TypeError):
        raise SigningError(
            "The stored certificate is damaged. Upload it again.", "CERTIFICATE_UNREADABLE", 409
        ) from None


def rotate(settings: Settings, base: Path) -> tuple[int, int]:
    """Re-encrypts the stored certificate with the first key of DATA_ENCRYPTION_KEY (for
    `rustdesk-api rotate-data-key`). Returns (re-encrypted, unreadable)."""
    path = _folder(base) / SECRET_FILE
    if not path.is_file():
        return 0, 0
    box = _box(settings)
    rotated = box.rotate(path.read_text(encoding="ascii"))
    if rotated is None:
        return 0, 1
    _replace(path, rotated.encode("ascii"))
    return 1, 0


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def find_tool(settings: Settings) -> str | None:
    configured = settings.installer_osslsigncode.strip()
    if configured:
        return configured if Path(configured).is_file() else shutil.which(configured)
    return shutil.which("osslsigncode")


def timestamp_host(settings: Settings) -> str | None:
    url = settings.installer_timestamp_url.strip()
    return (urlsplit(url).hostname or None) if url else None


def _tail(text: str, *hide: str) -> str:
    for secret in hide:
        if secret:
            text = text.replace(secret, "<hidden>")
    return "\n".join([line for line in text.splitlines() if line.strip()][-OUTPUT_TAIL_LINES:])


def sign_file(settings: Settings, base: Path, target: Path) -> None:
    """Signs `target` in place with the stored certificate. Raises SigningError."""
    tool = find_tool(settings)
    if tool is None:
        raise SigningError(
            "osslsigncode was not found on the server, so it cannot sign. Install it or set "
            "INSTALLER_OSSLSIGNCODE.",
            "SIGNING_TOOL_NOT_FOUND",
            409,
        )
    pfx, password = _load(settings, base)
    with tempfile.TemporaryDirectory(prefix="rd-sign-") as scratch:
        folder = Path(scratch)
        certificate = folder / "certificate.p12"
        password_file = folder / "password.txt"
        signed = folder / "signed.exe"
        _write_private(certificate, pfx)
        _write_private(password_file, password.encode("ascii"))
        argv = [
            tool,
            "sign",
            "-pkcs12",
            str(certificate),
            "-readpass",
            str(password_file),
            "-h",
            "sha256",
            "-n",
            "RustDesk",
            "-in",
            str(target),
            "-out",
            str(signed),
        ]
        if settings.installer_timestamp_url.strip():
            argv += ["-ts", settings.installer_timestamp_url.strip()]
        try:
            completed = _run(  # noqa: S603 - fixed argv, no shell; every path is ours
                argv, capture_output=True, timeout=SIGN_TIMEOUT_SECONDS, check=False
            )
        except subprocess.TimeoutExpired:
            raise SigningError(
                "Signing took too long and was stopped (is the timestamp server reachable?)."
            ) from None
        except OSError as exc:
            raise SigningError(f"osslsigncode could not be started ({type(exc).__name__}).") from None
        output = _tail(
            (completed.stdout + b"\n" + completed.stderr).decode("utf-8", errors="replace"),
            password,
            str(certificate),
            str(password_file),
        )
        if completed.returncode != 0 or not signed.is_file():
            logger.warning("osslsigncode failed (exit %s): %s", completed.returncode, output)
            hint = (
                " If the timestamp server cannot be reached, point INSTALLER_TIMESTAMP_URL at another one "
                "or leave it empty."
                if settings.installer_timestamp_url.strip()
                else ""
            )
            raise SigningError(f"osslsigncode failed (exit code {completed.returncode}).{hint}\n{output}")
        # The scratch folder may be on another disk than the installer: copy beside it, then swap.
        beside = target.with_name(target.name + ".signing")
        try:
            shutil.copyfile(signed, beside)
            os.replace(beside, target)
        finally:
            beside.unlink(missing_ok=True)
