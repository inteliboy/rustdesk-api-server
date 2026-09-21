"""Which build of the server this is, for the Dashboard and `/api/version`.

The commit comes from the `GIT_COMMIT` setting (the Docker image gets it as a
build argument, since `.git` is not copied into the image) and otherwise from
the git checkout the code is running out of. Neither is required: without one
the commit is simply reported as unknown.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rustdesk_api import __version__

logger = logging.getLogger(__name__)

REPOSITORY_URL = "https://github.com/inteliboy/rustdesk-api-server"

# The RustDesk client release whose source the protocol code was checked
# against (docs/rustdesk-compatibility.md). Bump it when the endpoints are
# re-verified against a newer client, not when a newer client merely exists.
RUSTDESK_CLIENT_SOURCE = "1.4.9"

_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_CHECKOUT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class BuildInfo:
    version: str
    commit: str | None  # full hash, None when unknown
    dirty: bool  # running from a checkout with uncommitted changes
    commit_url: str | None
    repository_url: str
    rustdesk_client_source: str

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "commit": self.commit,
            "commit_short": self.commit[:7] if self.commit else None,
            "dirty": self.dirty,
            "commit_url": self.commit_url,
            "repository_url": self.repository_url,
            "rustdesk_client_source": self.rustdesk_client_source,
        }


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(_CHECKOUT), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None  # git is not installed or hung: the commit is just unknown
    return done.stdout.strip() if done.returncode == 0 else None


@lru_cache(maxsize=1)
def _checkout_commit() -> tuple[str | None, bool]:
    if not (_CHECKOUT / ".git").exists():
        return None, False
    commit = _git("rev-parse", "HEAD")
    if not commit or not _SHA.match(commit):
        return None, False
    return commit, bool(_git("status", "--porcelain"))


def get_build_info(configured_commit: str = "") -> BuildInfo:
    commit = configured_commit.strip().lower()
    dirty = False
    if _SHA.match(commit):
        pass
    else:
        if commit and commit != "unknown":
            logger.warning("GIT_COMMIT is not a commit hash; ignoring it")
        commit_or_none, dirty = _checkout_commit()
        commit = commit_or_none or ""
    return BuildInfo(
        version=__version__,
        commit=commit or None,
        dirty=dirty,
        commit_url=f"{REPOSITORY_URL}/commit/{commit}" if commit else None,
        repository_url=REPOSITORY_URL,
        rustdesk_client_source=RUSTDESK_CLIENT_SOURCE,
    )
