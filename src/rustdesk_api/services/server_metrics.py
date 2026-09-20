"""What this server runs on, and how much of it the API server process uses.

Two separate things:

* `describe_host` - hardware and software facts for the admin "Server" tab.
  The slow-to-find, never-changing ones (CPU model, OS, package versions) are
  looked up once; uptime, disk and database size are read on every call.
* `MetricsSampler` - a rolling in-memory window of CPU and memory samples,
  filled by a background task. Nothing is persisted: a restart starts an empty
  window, and with several worker processes each keeps its own.

psutil is what makes CPU/memory usage readable on Windows, Linux and macOS
alike (the standard library has no CPU usage at all on Windows).

Only facts an administrator needs are exposed. No environment variables, no
executable or home paths, no database URL (it can carry credentials).
"""

from __future__ import annotations

import collections
import datetime
import functools
import importlib.metadata
import logging
import platform
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil

from rustdesk_api import __version__
from rustdesk_api.config import Settings

logger = logging.getLogger(__name__)

_SQLITE_PREFIX = "sqlite:///"
_PACKAGES = ("fastapi", "starlette", "uvicorn", "sqlalchemy", "alembic", "pydantic", "psutil")


@dataclass(frozen=True)
class Sample:
    """One reading. CPU figures are a percentage of the *whole* machine (100 =
    every logical core busy), so they compare across machines and never exceed
    100. `t` is Unix seconds."""

    t: float
    process_cpu: float
    system_cpu: float
    process_memory: int  # resident set size, bytes
    system_memory_percent: float
    threads: int


class MetricsSampler:
    def __init__(self, interval_seconds: int, history_minutes: int) -> None:
        self.interval_seconds = interval_seconds
        self.history_minutes = history_minutes
        self._process = psutil.Process()
        self._logical_cores = psutil.cpu_count(logical=True) or 1
        self._samples: collections.deque[Sample] = collections.deque(
            maxlen=max(1, history_minutes * 60 // interval_seconds)
        )
        self._lock = threading.Lock()
        # Both psutil CPU readings are "since the previous call"; the first call
        # only sets the baseline (it always returns 0.0), so make it now and
        # keep that reading out of the history.
        self._process.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None)

    def sample(self) -> Sample:
        with self._process.oneshot():
            process_cpu = self._process.cpu_percent(interval=None) / self._logical_cores
            memory = self._process.memory_info().rss
            threads = self._process.num_threads()
        reading = Sample(
            t=round(time.time(), 3),
            process_cpu=round(min(max(process_cpu, 0.0), 100.0), 1),
            system_cpu=round(psutil.cpu_percent(interval=None), 1),
            process_memory=memory,
            system_memory_percent=round(psutil.virtual_memory().percent, 1),
            threads=threads,
        )
        with self._lock:
            self._samples.append(reading)
        return reading

    def samples(self, since: float | None = None) -> list[Sample]:
        """Oldest first; only those newer than `since` (Unix seconds) when given."""
        with self._lock:
            items = list(self._samples)
        return items if since is None else [s for s in items if s.t > since]


@functools.lru_cache(maxsize=1)
def _cpu_model() -> str:
    """The processor's marketing name. `platform.processor()` alone is useless on
    Windows ("Intel64 Family 6 Model 158 ...") and empty on most Linux, so ask
    each platform where it keeps the real one."""
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            ) as key:
                return " ".join(str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).split())
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            return " ".join(out.stdout.split())
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition(":")
            if key.strip().lower() in ("model name", "hardware", "cpu model", "model"):
                return " ".join(value.split())
    except (OSError, ImportError, subprocess.SubprocessError) as exc:
        logger.debug("Could not read the CPU model: %s", exc)
    return platform.processor() or platform.machine() or "Unknown"


@functools.lru_cache(maxsize=1)
def _os_description() -> dict[str, str]:
    system = platform.system()
    if system == "Windows":
        edition = platform.win32_edition() if hasattr(platform, "win32_edition") else ""
        name = f"Windows {platform.release()}" + (f" {edition}" if edition else "")
        return {"name": name, "version": platform.version(), "kernel": "NT"}
    if system == "Darwin":
        return {
            "name": f"macOS {platform.mac_ver()[0]}".strip(),
            "version": platform.release(),
            "kernel": "Darwin",
        }
    name = system or "Unknown"
    try:
        name = platform.freedesktop_os_release().get("PRETTY_NAME", name)
    except OSError as exc:  # no /etc/os-release (minimal container, BSD)
        logger.debug("No os-release information: %s", exc)
    return {"name": name, "version": platform.release(), "kernel": system}


@functools.lru_cache(maxsize=1)
def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in _PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return versions


def _in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _database_path(settings: Settings) -> Path | None:
    url = settings.database_url
    return Path(url[len(_SQLITE_PREFIX) :]) if url.startswith(_SQLITE_PREFIX) else None


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _disk(path: Path) -> dict[str, int] | None:
    """Usage of the volume holding `path` (the nearest existing parent)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = psutil.disk_usage(str(probe))
    except OSError as exc:
        logger.debug("Could not read disk usage: %s", exc)
        return None
    return {"total": usage.total, "used": usage.used, "free": usage.free}


def _utc_offset() -> str:
    """ "+02:00". `time.tzname` is a localized, sometimes non-ASCII, name."""
    offset = datetime.datetime.now().astimezone().utcoffset() or datetime.timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _iso(timestamp: float) -> str:
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat()


def describe_host(settings: Settings) -> dict[str, Any]:
    process = psutil.Process()
    started = process.create_time()
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    frequency = psutil.cpu_freq()
    db_path = _database_path(settings)

    os_info = _os_description()
    hardware: dict[str, Any] = {
        "cpu_model": _cpu_model(),
        "cpu_physical_cores": psutil.cpu_count(logical=False),
        "cpu_logical_cores": psutil.cpu_count(logical=True),
        "cpu_max_mhz": round(frequency.max) if frequency and frequency.max else None,
        "architecture": platform.machine(),
        "memory_total": memory.total,
        "swap_total": swap.total,
        "data_disk": _disk(db_path.parent if db_path else Path.cwd()),
    }
    system = {
        "hostname": platform.node(),
        "os": os_info["name"],
        "os_version": os_info["version"],
        "kernel": os_info["kernel"],
        "container": _in_container(),
        "boot_time": _iso(psutil.boot_time()),
        "utc_offset": _utc_offset(),
    }
    software = {
        "app_version": __version__,
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "packages": _package_versions(),
        "sqlite": sqlite3.sqlite_version,
        "tls": bool(settings.ssl_certfile and settings.ssl_keyfile),
    }
    database: dict[str, Any] = {"engine": settings.database_url.split(":", 1)[0].split("+", 1)[0]}
    if db_path is not None:
        database["size_bytes"] = _file_size(db_path)
        # WAL mode keeps recent writes in a side file until a checkpoint.
        database["wal_size_bytes"] = _file_size(db_path.with_name(db_path.name + "-wal"))
    return {
        "hardware": hardware,
        "system": system,
        "software": software,
        "database": database,
        "process": {
            "pid": process.pid,
            "started_at": _iso(started),
            "uptime_seconds": max(0, int(time.time() - started)),
        },
    }


def sample_to_dict(sample: Sample) -> dict[str, Any]:
    return asdict(sample)
