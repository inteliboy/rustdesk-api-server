"""Is this the latest build of the project? (Dashboard, administrators only.)

Two questions, both answered by asking GitHub with anonymous requests that carry nothing about
this installation (no version, no address book, no device data; GitHub sees this server's IP
address and a User-Agent):

- Source: how far the running commit is from the tip of `main`, by GitHub's compare API. The
  running commit is the one baked into the Docker image, or the git checkout's (buildinfo.py).
- Image: when this is a container and there is a newer commit, whether its image is already
  published to ghcr.io (tag `sha-<7 characters>`). CI needs a few minutes after a push, so "a newer
  commit exists" and "a newer image exists" are not the same thing.

The answer is cached for hours (a failure for less), GitHub allows 60 anonymous requests an hour,
and `UPDATE_CHECK_ENABLED=false` makes no request at all.
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from dataclasses import asdict, dataclass
from urllib.parse import quote, urlsplit

import httpx

from rustdesk_api import __version__, buildinfo

logger = logging.getLogger(__name__)

SUCCESS_TTL_SECONDS = 6 * 3600
FAILURE_TTL_SECONDS = 30 * 60
# A forced re-check ("Check again") is allowed at most this often, whoever asks.
MIN_RECHECK_SECONDS = 60
GITHUB_API = "https://api.github.com"
GHCR = "https://ghcr.io"
BRANCH = "main"
# What ghcr.io serves a tag as: an OCI or Docker manifest (list).
MANIFEST_TYPES = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

# Tests point this at an in-process fake of GitHub and ghcr.io.
_transport: httpx.BaseTransport | None = None
_lock = threading.Lock()
_cache: tuple[float, float, Status] | None = None  # (time it was fetched, its ttl, the answer)


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport, _cache
    _transport = transport
    _cache = None


@dataclass
class Latest:
    commit: str
    date: str | None
    message: str | None
    url: str


@dataclass
class Status:
    # off | unknown | current | behind | ahead | diverged | unpublished | error
    state: str
    checked_at: str | None = None
    running_commit: str | None = None
    running_version: str | None = None
    dirty: bool = False
    in_image: bool = False
    behind_by: int | None = None
    latest: Latest | None = None
    # For a container with a newer commit: published | pending | unknown.
    image: str | None = None
    image_tag: str | None = None
    compare_url: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _repository() -> str:
    """owner/name of the project, from its public address."""
    return urlsplit(buildinfo.REPOSITORY_URL).path.strip("/")


def _client() -> httpx.Client:
    return httpx.Client(
        transport=_transport,
        timeout=httpx.Timeout(8.0, connect=5.0),
        follow_redirects=True,
        headers={"User-Agent": f"rustdesk-api-server/{__version__}"},
    )


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _rate_limited(response: httpx.Response) -> bool:
    return response.status_code in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0"


def _compare(client: httpx.Client, status: Status, running: str) -> Status:
    repo = _repository()
    response = client.get(
        f"{GITHUB_API}/repos/{repo}/compare/{running}...{BRANCH}",
        headers={"Accept": "application/vnd.github+json"},
    )
    if response.status_code == 404:
        # GitHub does not know this commit: a build made from local, unpushed work.
        status.state = "unpublished"
        return status
    if _rate_limited(response):
        raise UpdateCheckError(
            "GitHub's limit for anonymous requests is used up; it will work again within the hour."
        )
    if response.status_code != 200:
        raise UpdateCheckError(f"GitHub answered {response.status_code}.")
    data = response.json()
    result = data.get("status")
    status.compare_url = data.get("html_url")
    commits = data.get("commits") or []
    tip = commits[-1] if commits else None
    if tip:
        status.latest = Latest(
            commit=tip["sha"],
            date=(tip.get("commit", {}).get("committer") or {}).get("date"),
            message=(tip.get("commit", {}).get("message") or "").split("\n", 1)[0][:200] or None,
            url=tip.get("html_url") or f"{buildinfo.REPOSITORY_URL}/commit/{tip['sha']}",
        )
    if result == "identical":
        status.state = "current"
    elif result == "ahead":  # main is ahead of us: we are behind
        status.state = "behind"
        status.behind_by = int(data.get("ahead_by") or 0)
    elif result == "behind":  # main is behind us: this build has commits main does not
        status.state = "ahead"
    elif result == "diverged":
        status.state = "diverged"
        status.behind_by = int(data.get("ahead_by") or 0)
    else:
        raise UpdateCheckError(f"GitHub gave an unexpected comparison result ({result!r}).")
    return status


def _image_published(client: httpx.Client, tag: str) -> str:
    """published | pending, or unknown when ghcr.io cannot say (an anonymous pull token, then a
    manifest lookup for the tag)."""
    repo = _repository()
    token_response = client.get(
        f"{GHCR}/token", params={"service": "ghcr.io", "scope": f"repository:{repo}:pull"}
    )
    if token_response.status_code != 200 or not token_response.json().get("token"):
        return "unknown"
    token = token_response.json()["token"]
    manifest = client.head(
        f"{GHCR}/v2/{repo}/manifests/{quote(tag)}",
        headers={"Authorization": f"Bearer {token}", "Accept": MANIFEST_TYPES},
    )
    if manifest.status_code == 200:
        return "published"
    if manifest.status_code == 404:
        return "pending"
    return "unknown"


class UpdateCheckError(Exception):
    pass


def _check(enabled: bool, configured_commit: str) -> Status:
    if not enabled:
        return Status(state="off")
    info = buildinfo.get_build_info(configured_commit)
    status = Status(
        state="unknown",
        checked_at=_now(),
        running_commit=info.commit,
        running_version=info.version,
        dirty=info.dirty,
        in_image=buildinfo.is_image(),
    )
    if not info.commit:
        return status
    try:
        with _client() as client:
            _compare(client, status, info.commit)
            if status.state in ("behind", "diverged") and status.in_image and status.latest:
                status.image_tag = f"sha-{status.latest.commit[:7]}"
                try:
                    status.image = _image_published(client, status.image_tag)
                except (httpx.HTTPError, ValueError) as exc:
                    logger.info("Could not look up the image tag on ghcr.io: %s", exc)
                    status.image = "unknown"
    except UpdateCheckError as exc:
        status.state, status.error = "error", str(exc)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.info("Update check failed: %s", exc)
        status.state, status.error = "error", "GitHub could not be reached."
    return status


def get_status(enabled: bool, configured_commit: str = "", *, refresh: bool = False) -> Status:
    """The cached answer, or a fresh one when it is older than its time to live (a forced refresh
    is honoured no more than once a minute)."""
    global _cache
    if not enabled:
        return Status(state="off")
    with _lock:
        cached = _cache
        now = time.monotonic()
        if cached:
            age = now - cached[0]
            if age < (MIN_RECHECK_SECONDS if refresh else cached[1]):
                return cached[2]
        status = _check(enabled, configured_commit)
        ttl = FAILURE_TTL_SECONDS if status.state == "error" else SUCCESS_TTL_SECONDS
        _cache = (now, ttl, status)
        return status
