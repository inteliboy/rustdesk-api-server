"""A Windows setup .exe that installs RustDesk and applies this server's settings.

The setup file is an NSIS wrapper around RustDesk's own MSI: it removes an existing
installation, installs the MSI silently, runs `rustdesk.exe --config <string>` and
installs the service. That is the same recipe as a hand-made rustdesk.nsi; here the
server writes the script, downloads the MSI and runs `makensis`.

Two ways to get the file:

* **Build on the server** (`start_build`): needs `makensis` on the server and outbound
  HTTPS to GitHub. The result can be signed on the spot with INSTALLER_SIGN_COMMAND.
* **Build kit** (`build_kit`): a zip with the script and a PowerShell script that does the
  same on any Windows machine, so the code-signing certificate can stay on the machine
  that holds it.

Everything that reaches the generated files is validated first (release tags, file names,
digests, the config string is URL-safe base64), and the only hosts contacted are
github.com and raw.githubusercontent.com (their download redirects go to GitHub's own
content hosts). The MSI is checked against the sha256 digest GitHub publishes.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from rustdesk_api.config import Settings
from rustdesk_api.services import backup as backup_service
from rustdesk_api.services import client_config

logger = logging.getLogger(__name__)

GITHUB_RELEASES_URL = "https://api.github.com/repos/rustdesk/rustdesk/releases?per_page=30"
GITHUB_RAW_URL = "https://raw.githubusercontent.com/rustdesk/rustdesk"
RELEASE_DOWNLOAD_PREFIX = "/rustdesk/rustdesk/releases/download/"

# The name in the WebUI -> the name in the release's file names.
ARCHES = {"x64": "x86_64", "arm64": "aarch64"}

RELEASES_TTL_SECONDS = 600
MAX_MSI_BYTES = 200 * 1024 * 1024
MAX_ICON_BYTES = 1024 * 1024
BUILD_TIMEOUT_SECONDS = 300
KEEP_MSI_FILES = 6
JOB_LIFETIME_SECONDS = 3600
MAX_JOBS = 20
OUTPUT_TAIL_LINES = 12

_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MSI_NAME = re.compile(
    r"^rustdesk-(?P<version>[A-Za-z0-9][A-Za-z0-9._-]{0,63})-(?P<arch>x86_64|aarch64)\.msi$"
)
_DIGEST = re.compile(r"^sha256:(?P<hex>[0-9a-f]{64})$")
_CONFIG_STRING = re.compile(r"^[A-Za-z0-9_=-]{1,2048}$")


class InstallerError(Exception):
    """The installer could not be built; the message is safe to show (no URLs with
    credentials, no signing command)."""

    def __init__(self, message: str, code: str = "INSTALLER_FAILED", status: int = 502):
        super().__init__(message)
        self.code = code
        self.status = status


class InstallerBusy(InstallerError):
    def __init__(self) -> None:
        super().__init__("Another installer is being built. Wait for it to finish.", "INSTALLER_BUSY", 409)


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str | None  # lower-case hex, when GitHub publishes it


@dataclass(frozen=True)
class Release:
    tag: str
    prerelease: bool
    published_at: str
    version: str
    assets: dict[str, Asset]  # keyed by the WebUI's architecture name ("x64", "arm64")


def parse_releases(payload: object) -> list[Release]:
    """The releases that have at least one MSI, in the order GitHub sent them (newest
    first). Anything unexpected in an item skips that item rather than the whole list."""
    if not isinstance(payload, list):
        raise InstallerError("GitHub answered with something unexpected.")
    releases: list[Release] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        tag = item.get("tag_name")
        if not isinstance(tag, str) or not _TAG.match(tag):
            continue
        assets: dict[str, Asset] = {}
        version = ""
        for raw in item.get("assets") or []:
            if not isinstance(raw, dict):
                continue
            name, url = raw.get("name"), raw.get("browser_download_url")
            match = _MSI_NAME.match(name) if isinstance(name, str) else None
            if not match or not isinstance(name, str) or not isinstance(url, str) or not _is_release_url(url):
                continue
            digest = _DIGEST.match(raw.get("digest") or "")
            size = raw.get("size")
            arch = next(key for key, value in ARCHES.items() if value == match["arch"])
            assets[arch] = Asset(
                name=name,
                url=url,
                size=size if isinstance(size, int) and size > 0 else 0,
                sha256=digest["hex"] if digest else None,
            )
            version = match["version"]
        if assets:
            published = item.get("published_at")
            releases.append(
                Release(
                    tag=tag,
                    prerelease=bool(item.get("prerelease")),
                    published_at=published if isinstance(published, str) else "",
                    version=version,
                    assets=assets,
                )
            )
    return releases


def _is_release_url(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname == "github.com"
        and parts.path.startswith(RELEASE_DOWNLOAD_PREFIX)
    )


# Tests point this at an in-process fake of GitHub.
_transport: httpx.BaseTransport | None = None
_releases_lock = threading.Lock()
_releases_cache: tuple[float, list[Release]] | None = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport, _releases_cache
    _transport = transport
    _releases_cache = None


def _client() -> httpx.Client:
    return httpx.Client(
        transport=_transport,
        timeout=httpx.Timeout(60.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": "rustdesk-api-server", "Accept": "application/vnd.github+json"},
    )


def list_releases() -> list[Release]:
    """Cached for a few minutes (GitHub allows 60 anonymous requests an hour); a stale copy
    is served if GitHub cannot be reached."""
    global _releases_cache
    with _releases_lock:
        cached = _releases_cache
        if cached and time.monotonic() - cached[0] < RELEASES_TTL_SECONDS:
            return cached[1]
        try:
            with _client() as client:
                response = client.get(GITHUB_RELEASES_URL)
            if response.status_code != 200:
                raise InstallerError(
                    f"GitHub answered {response.status_code} when the releases were requested."
                )
            releases = parse_releases(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            if cached:
                logger.warning(
                    "Could not refresh the RustDesk release list (%s); using the last one", type(exc).__name__
                )
                return cached[1]
            raise InstallerError("GitHub could not be reached to list the RustDesk releases.") from exc
        except InstallerError:
            if cached:
                return cached[1]
            raise
        _releases_cache = (time.monotonic(), releases)
        return releases


def find_release(tag: str) -> Release:
    for release in list_releases():
        if release.tag == tag:
            return release
    raise InstallerError("That RustDesk release does not exist.", "RELEASE_NOT_FOUND", 404)


def pick_asset(release: Release, arch: str) -> Asset:
    asset = release.assets.get(arch)
    if asset is None:
        raise InstallerError(f"RustDesk {release.tag} has no MSI for {arch}.", "ARCH_NOT_AVAILABLE", 409)
    return asset


# ---------------------------------------------------------------------------
# Where things live and what is available
# ---------------------------------------------------------------------------


def installer_directory(settings: Settings) -> Path:
    if settings.installer_dir.strip():
        return Path(settings.installer_dir.strip())
    source = backup_service.sqlite_path(settings)
    return (source.parent if source is not None else Path("data")) / "installers"


def find_makensis(settings: Settings) -> str | None:
    configured = settings.installer_makensis.strip()
    if configured:
        return configured if Path(configured).is_file() else shutil.which(configured)
    found = shutil.which("makensis")
    if found:
        return found
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(variable)
        if base and (candidate := Path(base) / "NSIS" / "makensis.exe").is_file():
            return str(candidate)
    return None


def availability(settings: Settings) -> str | None:
    """None when the server can build installers, otherwise why not ('disabled' or 'no_makensis')."""
    if not settings.installer_build_enabled:
        return "disabled"
    if find_makensis(settings) is None:
        return "no_makensis"
    return None


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _prune(directory: Path, pattern: str, keep: int) -> None:
    files = sorted(directory.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError as exc:
            logger.warning("Could not delete the old download %s: %s", stale.name, exc)


def fetch_msi(settings: Settings, asset: Asset, progress=None) -> Path:
    """The MSI in the cache, downloading it first if needed. The digest is part of the cache
    name, so a nightly build that changed is fetched again; a file is only reused when
    GitHub published a digest to tie it to."""
    directory = installer_directory(settings) / "msi"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = asset.sha256[:16] if asset.sha256 else "nodigest"
    target = directory / f"{stamp}-{_safe_name(asset.name)}"
    if asset.sha256 and target.is_file() and target.stat().st_size > 0:
        return target
    partial = target.with_suffix(target.suffix + ".partial")
    digest = hashlib.sha256()
    received = 0
    try:
        with _client() as client, client.stream("GET", asset.url) as response:
            if response.status_code != 200:
                raise InstallerError(f"GitHub answered {response.status_code} for the RustDesk MSI.")
            if response.url.scheme != "https":
                raise InstallerError("The download was redirected to a plain HTTP address; it was refused.")
            with partial.open("wb") as out:
                for chunk in response.iter_bytes(64 * 1024):
                    received += len(chunk)
                    if received > MAX_MSI_BYTES:
                        raise InstallerError("The download is larger than expected; it was stopped.")
                    digest.update(chunk)
                    out.write(chunk)
                    if progress and asset.size:
                        progress(min(99, received * 100 // asset.size))
        if asset.sha256 and digest.hexdigest() != asset.sha256:
            raise InstallerError(
                "The downloaded MSI does not match the checksum GitHub published; it was discarded."
            )
        if not received:
            raise InstallerError("The downloaded MSI is empty.")
        os.replace(partial, target)
    except httpx.HTTPError as exc:
        raise InstallerError("The RustDesk MSI could not be downloaded from GitHub.") from exc
    finally:
        partial.unlink(missing_ok=True)
    _prune(directory, "*.msi", KEEP_MSI_FILES)
    return target


def _looks_like_icon(data: bytes) -> bool:
    return len(data) > 22 and data[:4] == b"\x00\x00\x01\x00"


def fetch_icon(settings: Settings, tag: str) -> Path | None:
    """The icon for the setup file, or None (the file then gets NSIS's default icon).
    INSTALLER_ICON wins; otherwise RustDesk's own icon from the release's source tree."""
    configured = settings.installer_icon.strip()
    if configured:
        path = Path(configured)
        try:
            if path.is_file() and _looks_like_icon(path.read_bytes()[:32]):
                return path
        except OSError as exc:
            logger.warning("INSTALLER_ICON could not be read: %s", exc)
        else:
            logger.warning("INSTALLER_ICON is not a readable .ico file; it is ignored")
        return None
    directory = installer_directory(settings) / "msi"
    directory.mkdir(parents=True, exist_ok=True)
    refs = [tag, "master"] if tag != "nightly" else ["master"]
    for ref in refs:
        target = directory / f"icon-{_safe_name(ref)}.ico"
        if target.is_file() and target.stat().st_size > 0:
            return target
        try:
            with _client() as client:
                response = client.get(f"{GITHUB_RAW_URL}/{ref}/res/icon.ico")
        except httpx.HTTPError:
            continue  # the icon is cosmetic: try the next ref, then do without
        if (
            response.status_code == 200
            and len(response.content) <= MAX_ICON_BYTES
            and _looks_like_icon(response.content)
        ):
            target.write_bytes(response.content)
            return target
    return None


# ---------------------------------------------------------------------------
# The NSIS script
# ---------------------------------------------------------------------------


def _nsis(text: str) -> str:
    """A value inside an NSIS double-quoted string."""
    return text.replace("$", "$$").replace('"', '$\\"')


_RESET_LINES = (
    '    RMDir /r "$APPDATA\\RustDesk"\n'
    '    RMDir /r "$LOCALAPPDATA\\rustdesk"\n'
    '    RMDir /r "$WINDIR\\ServiceProfiles\\LocalService\\AppData\\Roaming\\RustDesk"\n'
)

_NSI_TEMPLATE = r"""; RustDesk setup for @@SERVER@@ - generated by the RustDesk API server.
; Installs RustDesk @@VERSION@@ (@@ARCH@@) silently and applies the server settings.
; Build: makensis rustdesk.nsi   (see README.txt when this came in a build kit)
!define APPNAME "RustDeskInstaller"
!define MSI_FILE "rustdesk.msi"
!define INSTALL_DIR "C:\Program Files\RustDesk"
!define EXECUTABLE "rustdesk.exe"
!define CONFIG_ARGS "--config @@CONFIG@@"
!define UNINSTALL_SHORTCUT "${INSTALL_DIR}\Uninstall RustDesk.lnk"
@@FINALIZE@@
Outfile "@@OUTFILE@@"
RequestExecutionLevel admin
SilentInstall silent
@@ICON@@
Section "Install"

    UserInfo::GetAccountType
    Pop $0
    StrCmp $0 "Admin" 0 AdminRequired

    ; An installed RustDesk is removed first, so the version installed is the one chosen here.
    IfFileExists "${UNINSTALL_SHORTCUT}" 0 SkipUninstall

    ExecWait '"${INSTALL_DIR}\${EXECUTABLE}" --uninstall'
@@RESET@@    Sleep 5000

SkipUninstall:
    File /oname=$TEMP\${MSI_FILE} "@@MSI@@"

    ExecWait 'msiexec /i "$TEMP\${MSI_FILE}" /qn INSTALLFOLDER="${INSTALL_DIR}" CREATESTARTMENUSHORTCUTS="N" CREATEDESKTOPSHORTCUTS="Y"' $0
    Delete $TEMP\${MSI_FILE}
    ; 0 = installed, 3010 = installed and Windows wants a restart.
    StrCmp $0 0 Installed
    StrCmp $0 3010 Installed
    Abort "Installation failed!"

Installed:
    Sleep 5000
    ExecWait '"${INSTALL_DIR}\${EXECUTABLE}" ${CONFIG_ARGS}'

    Sleep 5000
    ExecWait '"${INSTALL_DIR}\${EXECUTABLE}" --install-service'
    Goto Done

AdminRequired:
    MessageBox MB_ICONSTOP "This installer requires administrator privileges!"
    Abort

Done:
SectionEnd
"""  # noqa: E501


NL = chr(10)


def _icon_block(icon: str) -> str:
    """The Icon line, guarded so a missing file (a kit whose icon download failed) means
    the default icon rather than a failed build."""
    if not icon:
        return ""
    path = _nsis(icon)
    return f'!if /FileExists "{path}"' + NL + f'Icon "{path}"' + NL + "!endif" + NL


@dataclass(frozen=True)
class ScriptSpec:
    servers: client_config.ClientServers
    version: str
    arch: str  # the WebUI's name: "x64" or "arm64"
    msi: str  # the path NSIS reads the MSI from
    icon: str  # the path of a .ico; used only if the file exists when NSIS runs
    outfile: str
    reset_settings: bool = False
    sign_command: str = ""


def render_nsi(spec: ScriptSpec) -> str:
    """The NSIS script. Every value is validated or escaped: the config string is URL-safe
    base64, the version comes from a release file name, the paths are ours."""
    config = client_config.config_string(spec.servers)
    if not _CONFIG_STRING.match(config):
        raise InstallerError(
            "The server settings could not be turned into a config string.", "BAD_CONFIG", 422
        )
    if spec.arch not in ARCHES or not re.match(r"^[A-Za-z0-9._-]{1,64}$", spec.version):
        raise InstallerError("Unexpected version or architecture.", "BAD_REQUEST", 422)
    # The command comes from the environment and Settings has checked it (one line, no backtick).
    finalize = f"!finalize `{spec.sign_command.replace('$', '$$')}`\n" if spec.sign_command else ""
    host = re.sub(r"[^A-Za-z0-9._:/-]", "", spec.servers.id_server)[:80]
    replacements = {
        "@@SERVER@@": host,
        "@@VERSION@@": spec.version,
        "@@ARCH@@": spec.arch,
        "@@CONFIG@@": config,
        "@@FINALIZE@@": finalize,
        "@@OUTFILE@@": _nsis(spec.outfile),
        "@@ICON@@": _icon_block(spec.icon),
        "@@MSI@@": _nsis(spec.msi),
        "@@RESET@@": _RESET_LINES if spec.reset_settings else "",
    }
    text = _NSI_TEMPLATE
    for token, value in replacements.items():
        text = text.replace(token, value)
    return text


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def _redact(output: str, *secrets_to_hide: str) -> str:
    for secret in secrets_to_hide:
        if secret:
            output = output.replace(secret, "<hidden>")
    return output


def run_makensis(makensis: str, script: Path, sign_command: str = "") -> str:
    """Runs makensis on the script. Its output can echo the signing command, so it is
    scrubbed before anything is kept."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell; the script path is ours
            [makensis, "-V2", str(script)],
            cwd=script.parent,
            capture_output=True,
            timeout=BUILD_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallerError("makensis took too long and was stopped.") from exc
    except OSError as exc:
        raise InstallerError(f"makensis could not be started ({type(exc).__name__}).") from exc
    text = _redact(
        (completed.stdout + b"\n" + completed.stderr).decode("utf-8", errors="replace"),
        sign_command,
        sign_command.replace("$", "$$"),
    )
    tail = "\n".join([line for line in text.splitlines() if line.strip()][-OUTPUT_TAIL_LINES:])
    if completed.returncode != 0:
        logger.warning("makensis failed (exit %s): %s", completed.returncode, tail)
        signing_hint = " Signing is configured, so check INSTALLER_SIGN_COMMAND too." if sign_command else ""
        raise InstallerError(f"makensis failed (exit code {completed.returncode}).{signing_hint}\n{tail}")
    return tail


@dataclass
class BuildSpec:
    servers: client_config.ClientServers
    tag: str
    arch: str
    reset_settings: bool = False


@dataclass
class Job:
    id: str
    tag: str
    arch: str
    state: str = "queued"  # queued, downloading, building, done, failed
    progress: int = 0
    message: str = ""
    version: str = ""
    filename: str = ""
    size: int = 0
    signed: bool = False
    warnings: list[str] = field(default_factory=list)
    file: Path | None = None
    created: float = field(default_factory=time.monotonic)


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()
_build_slot = threading.Lock()


def reset_state() -> None:
    """For tests: forget the jobs and the cached release list."""
    global _releases_cache
    with _jobs_lock:
        _jobs.clear()
    _releases_cache = None


def get_job(job_id: str) -> Job | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def _forget_old_jobs() -> None:
    """Drops jobs (and their files) that are an hour old, and the oldest ones beyond MAX_JOBS."""
    now = time.monotonic()
    with _jobs_lock:
        by_age = sorted(_jobs.values(), key=lambda job: job.created)
        expired = [job for job in by_age if now - job.created > JOB_LIFETIME_SECONDS]
        overflow = [job for job in by_age if job not in expired][
            : max(0, len(by_age) - len(expired) - (MAX_JOBS - 1))
        ]
        for job in expired + overflow:
            _jobs.pop(job.id, None)
            if job.file is not None:
                job.file.unlink(missing_ok=True)


def start_build(settings: Settings, spec: BuildSpec) -> Job:
    """Checks what can be checked at once (a release with that architecture exists, a
    builder is installed) and runs the rest in the background: the download and the build
    take longer than a reverse proxy waits. One build at a time."""
    reason = availability(settings)
    if reason == "disabled":
        raise InstallerError("Building installers on the server is turned off.", "INSTALLER_DISABLED", 409)
    if reason == "no_makensis":
        raise InstallerError(
            "NSIS (makensis) was not found on the server. Install it or set INSTALLER_MAKENSIS, "
            "or use the build kit.",
            "NSIS_NOT_FOUND",
            409,
        )
    release = find_release(spec.tag)
    asset = pick_asset(release, spec.arch)
    if not _build_slot.acquire(blocking=False):
        raise InstallerBusy
    try:
        _forget_old_jobs()
        job = Job(
            id=secrets.token_urlsafe(16),
            tag=release.tag,
            arch=spec.arch,
            version=release.version,
            warnings=client_config.warnings(spec.servers),
        )
        with _jobs_lock:
            _jobs[job.id] = job
        threading.Thread(target=_run_job, args=(settings, spec, release, asset, job), daemon=True).start()
    except BaseException:
        _build_slot.release()
        raise
    return job


def _run_job(settings: Settings, spec: BuildSpec, release: Release, asset: Asset, job: Job) -> None:
    try:
        job.state = "downloading"
        msi = fetch_msi(settings, asset, progress=lambda percent: setattr(job, "progress", percent))
        icon = fetch_icon(settings, release.tag)
        job.state, job.progress = "building", 100
        makensis = find_makensis(settings)
        if makensis is None:  # removed since the request was accepted
            raise InstallerError("NSIS (makensis) was not found on the server.")
        out_dir = installer_directory(settings) / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        outfile = out_dir / f"rustdesk-{release.version}-{ARCHES[spec.arch]}-{job.id[:10]}.exe"
        scratch = out_dir / f"{job.id}.build"
        scratch.mkdir()
        try:
            script = scratch / "rustdesk.nsi"
            script.write_text(
                render_nsi(
                    ScriptSpec(
                        servers=spec.servers,
                        version=release.version,
                        arch=spec.arch,
                        msi=str(msi.resolve()),
                        icon=str(icon.resolve()) if icon else "",
                        outfile=str(outfile.resolve()),
                        reset_settings=spec.reset_settings,
                        sign_command=settings.installer_sign_command,
                    )
                ),
                encoding="utf-8",
            )
            run_makensis(makensis, script, settings.installer_sign_command)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if not outfile.is_file():
            raise InstallerError("makensis finished but produced no file.")
        job.file = outfile
        job.size = outfile.stat().st_size
        job.signed = bool(settings.installer_sign_command)
        job.filename = f"rustdesk-{release.version}-{ARCHES[spec.arch]}-preconfigured.exe"
        job.state = "done"
        _prune_output(out_dir)
    except InstallerError as exc:
        job.state, job.message = "failed", str(exc)
    except Exception:  # noqa: BLE001 - reported to the person, logged with the traceback
        logger.exception("Building the installer failed unexpectedly")
        job.state, job.message = "failed", "The installer could not be built (see the server log)."
    finally:
        _build_slot.release()


def _prune_output(out_dir: Path) -> None:
    """Anything in the output folder that no job points at (a restart forgets the jobs) and
    is older than an hour."""
    live = {job.file for job in list(_jobs.values()) if job.file is not None}
    cutoff = time.time() - JOB_LIFETIME_SECONDS
    for path in out_dir.glob("*.exe"):
        try:
            if path not in live and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError as exc:
            logger.warning("Could not delete the old installer %s: %s", path.name, exc)


# ---------------------------------------------------------------------------
# What is stored on the server
# ---------------------------------------------------------------------------

# kind -> (sub-folder, glob). "msi" is the RustDesk MSI downloaded from GitHub, "installer" a setup
# file built here. Both can be rebuilt or downloaded again at any time.
FILE_KINDS = {"msi": ("msi", "*.msi"), "installer": ("out", "*.exe")}
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


@dataclass(frozen=True)
class StoredFile:
    kind: str
    name: str
    size: int
    modified: datetime.datetime


def list_files(settings: Settings) -> list[StoredFile]:
    """The downloaded MSIs and the built installers, newest first."""
    found: list[StoredFile] = []
    for kind, (folder, pattern) in FILE_KINDS.items():
        for path in (installer_directory(settings) / folder).glob(pattern):
            try:
                info = path.stat()
            except OSError:
                continue  # removed while listing
            if path.is_file() and not path.is_symlink():
                found.append(
                    StoredFile(
                        kind,
                        path.name,
                        info.st_size,
                        datetime.datetime.fromtimestamp(info.st_mtime, datetime.timezone.utc),
                    )
                )
    return sorted(found, key=lambda f: f.modified, reverse=True)


def stored_file_path(settings: Settings, kind: str, name: str) -> Path:
    """The path of one stored file. The name comes from a URL, so it must be a plain file name
    of the right type that exists in the folder (no separators, no links)."""
    if kind not in FILE_KINDS:
        raise InstallerError("Unknown kind of file.", "BAD_REQUEST", 422)
    folder, pattern = FILE_KINDS[kind]
    if not _FILE_NAME.match(name) or not Path(name).match(pattern):
        raise InstallerError("That file does not exist.", "FILE_NOT_FOUND", 404)
    path = installer_directory(settings) / folder / name
    if not path.is_file() or path.is_symlink():
        raise InstallerError("That file does not exist.", "FILE_NOT_FOUND", 404)
    return path


def delete_files(settings: Settings, kind: str, name: str | None = None) -> int:
    """Deletes one stored file, or every file of a kind when `name` is None; returns how many.
    Refused while a build runs (it may be reading the MSI or writing the installer)."""
    if kind not in FILE_KINDS:
        raise InstallerError("Unknown kind of file.", "BAD_REQUEST", 422)
    if _build_slot.locked():
        raise InstallerBusy
    if name is not None:
        targets = [stored_file_path(settings, kind, name)]
    else:
        folder, pattern = FILE_KINDS[kind]
        directory = installer_directory(settings) / folder
        targets = [p for p in directory.glob(pattern) if p.is_file() and not p.is_symlink()]
    deleted = 0
    for path in targets:
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path.name, exc)
            raise InstallerError(
                f"{path.name} could not be deleted ({type(exc).__name__}).", "DELETE_FAILED", 500
            ) from exc
    return deleted


# ---------------------------------------------------------------------------
# The build kit
# ---------------------------------------------------------------------------

_KIT_README = """RustDesk setup for {server} - build kit
========================================

RustDesk {version} ({arch}). Files:

  rustdesk.nsi   the NSIS script (installs the MSI, applies the server settings, installs the service)
  build.ps1      downloads the MSI, builds rustdesk-{version}-{msi_arch}-preconfigured.exe and can sign it

Needs Windows with NSIS installed (winget install NSIS.NSIS) and access to github.com.

  powershell -ExecutionPolicy Bypass -File .\\build.ps1

Sign the result with a certificate that is in this machine's certificate store (the thumbprint
is under Certificate > Details in certmgr.msc; the signing tool comes with the Windows SDK):

  powershell -ExecutionPolicy Bypass -File .\\build.ps1 -Thumbprint <THUMBPRINT>

Or with a .pfx file (you are asked for its password):

  powershell -ExecutionPolicy Bypass -File .\\build.ps1 -PfxFile C:\\path\\to\\certificate.pfx

Other options: -TimestampUrl (default http://timestamp.digicert.com), -SignTool <path to signtool.exe>,
-Makensis <path to makensis.exe>.

The setup file contains this server's ID server, relay, API server and public key. It contains no
password. Try it on one machine first: it removes an existing RustDesk installation before it installs.
"""

_KIT_SCRIPT = r"""<#
  Builds the RustDesk setup file for @@SERVER@@ (RustDesk @@VERSION@@, @@ARCH@@).
  Run this on the Windows machine that has the code-signing certificate, if you want it signed.
#>
[CmdletBinding()]
param(
    [string]$Thumbprint = '',
    [string]$PfxFile = '',
    [securestring]$PfxPassword,
    [string]$TimestampUrl = 'http://timestamp.digicert.com',
    [string]$SignTool = '',
    [string]$Makensis = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-Location -LiteralPath $PSScriptRoot

$msiUrl = '@@MSI_URL@@'
$msiSha256 = '@@MSI_SHA256@@'
$iconUrl = '@@ICON_URL@@'
$output = '@@OUTFILE@@'

function Find-Tool([string]$given, [string]$name, [string[]]$candidates) {
    if ($given) { if (Test-Path -LiteralPath $given) { return $given } else { throw "$given does not exist." } }
    $onPath = Get-Command $name -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    foreach ($candidate in $candidates) {
        $found = Get-ChildItem -Path $candidate -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

$nsis = Find-Tool $Makensis 'makensis.exe' @("${env:ProgramFiles(x86)}\NSIS\makensis.exe", "$env:ProgramFiles\NSIS\makensis.exe")
if (-not $nsis) { throw 'NSIS was not found. Install it with: winget install NSIS.NSIS' }

if (-not (Test-Path -LiteralPath 'rustdesk.msi') -or ($msiSha256 -and (Get-FileHash 'rustdesk.msi' -Algorithm SHA256).Hash -ne $msiSha256)) {
    Write-Host "Downloading $msiUrl"
    Invoke-WebRequest -Uri $msiUrl -OutFile 'rustdesk.msi' -UseBasicParsing
}
if ($msiSha256 -and (Get-FileHash 'rustdesk.msi' -Algorithm SHA256).Hash -ne $msiSha256) {
    Remove-Item 'rustdesk.msi'
    throw 'The downloaded MSI does not match the checksum GitHub published.'
}
if (-not (Test-Path -LiteralPath 'rustdesk.ico')) {
    try { Invoke-WebRequest -Uri $iconUrl -OutFile 'rustdesk.ico' -UseBasicParsing } catch { Write-Host 'No icon downloaded; the default icon is used.' }
}

if (Test-Path -LiteralPath $output) { Remove-Item -LiteralPath $output }
& $nsis '-V2' 'rustdesk.nsi'
if ($LASTEXITCODE -ne 0) { throw "makensis failed (exit code $LASTEXITCODE)." }

if ($Thumbprint -or $PfxFile) {
    $tool = Find-Tool $SignTool 'signtool.exe' @("${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe")
    if (-not $tool) { throw 'signtool.exe was not found. Install the Windows SDK, or pass -SignTool <path>.' }
    $arguments = @('sign', '/fd', 'SHA256', '/tr', $TimestampUrl, '/td', 'SHA256')
    if ($Thumbprint) {
        $arguments += @('/sha1', $Thumbprint)
    } else {
        if (-not $PfxPassword) { $PfxPassword = Read-Host -AsSecureString 'Password of the .pfx file' }
        $password = [System.Net.NetworkCredential]::new('', $PfxPassword).Password
        $arguments += @('/f', $PfxFile, '/p', $password)
    }
    & $tool @arguments $output
    if ($LASTEXITCODE -ne 0) { throw "signtool failed (exit code $LASTEXITCODE)." }
    $signature = Get-AuthenticodeSignature -LiteralPath $output
    Write-Host "Signature: $($signature.Status)"
}

Write-Host "Done: $((Resolve-Path -LiteralPath $output).Path)"
"""  # noqa: E501


def build_kit(
    servers: client_config.ClientServers, release: Release, arch: str, reset_settings: bool = False
) -> bytes:
    """A zip with the NSIS script and a PowerShell script that builds (and optionally signs)
    the setup file on the machine that runs it."""
    asset = pick_asset(release, arch)
    msi_arch = ARCHES[arch]
    outfile = f"rustdesk-{release.version}-{msi_arch}-preconfigured.exe"
    ref = "master" if release.tag == "nightly" else release.tag
    script = _KIT_SCRIPT
    for token, value in {
        "@@SERVER@@": re.sub(r"[^A-Za-z0-9._:/-]", "", servers.id_server)[:80],
        "@@VERSION@@": release.version,
        "@@ARCH@@": arch,
        "@@MSI_URL@@": asset.url,
        "@@MSI_SHA256@@": (asset.sha256 or "").upper(),
        "@@ICON_URL@@": f"{GITHUB_RAW_URL}/{ref}/res/icon.ico",
        "@@OUTFILE@@": outfile,
    }.items():
        script = script.replace(token, value)
    nsi = render_nsi(
        ScriptSpec(
            servers=servers,
            version=release.version,
            arch=arch,
            msi="rustdesk.msi",
            icon="rustdesk.ico",
            outfile=outfile,
            reset_settings=reset_settings,
        )
    )
    readme = _KIT_README.format(
        server=re.sub(r"[^A-Za-z0-9._:/-]", "", servers.id_server)[:80],
        version=release.version,
        arch=arch,
        msi_arch=msi_arch,
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in (("rustdesk.nsi", nsi), ("build.ps1", script), ("README.txt", readme)):
            # Windows tools are happiest with CRLF; the scripts are plain ASCII.
            archive.writestr(
                name, text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii", errors="replace")
            )
    return buffer.getvalue()
