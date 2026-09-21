from __future__ import annotations

import httpx
import pytest

from rustdesk_api import buildinfo
from rustdesk_api.services import updates

RUNNING = "a" * 40
LATEST = "b" * 40
REPO = "inteliboy/rustdesk-api-server"


class Fake:
    """GitHub and ghcr.io, in process. `requests` records every request made."""

    def __init__(self, *, compare=None, token=None, manifest=200, compare_status=200, compare_headers=None):
        self.requests: list[httpx.Request] = []
        self.compare = compare
        self.compare_status = compare_status
        self.compare_headers = compare_headers or {}
        self.token = {"token": "anonymous-pull-token"} if token is None else token
        self.manifest = manifest

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith("https://api.github.com/"):
            return httpx.Response(self.compare_status, json=self.compare or {}, headers=self.compare_headers)
        if url.startswith("https://ghcr.io/token"):
            return httpx.Response(200, json=self.token)
        if url.startswith("https://ghcr.io/v2/"):
            return httpx.Response(self.manifest)
        return httpx.Response(500)


def commits_ahead(n=3):
    tip = {
        "sha": LATEST,
        "html_url": f"https://github.com/{REPO}/commit/{LATEST}",
        "commit": {
            "message": "Add the web client\n\nlonger text",
            "committer": {"date": "2026-09-21T10:00:00Z"},
        },
    }
    return {
        "status": "ahead",
        "ahead_by": n,
        "html_url": f"https://github.com/{REPO}/compare/a...main",
        "commits": [tip],
    }


@pytest.fixture()
def running(monkeypatch):
    def set_build(commit=RUNNING, image=False, dirty=False):
        info = buildinfo.BuildInfo(
            version="0.2.0",
            commit=commit,
            dirty=dirty,
            commit_url=None,
            repository_url=buildinfo.REPOSITORY_URL,
            rustdesk_client_source="1.4.9",
        )
        monkeypatch.setattr(buildinfo, "get_build_info", lambda configured="": info)
        monkeypatch.setattr(buildinfo, "is_image", lambda: image)

    set_build()
    yield set_build
    updates.set_transport(None)


def use(fake: Fake) -> Fake:
    updates.set_transport(httpx.MockTransport(fake))
    return fake


def test_it_makes_no_request_when_switched_off(running):
    fake = use(Fake())
    assert updates.get_status(False).state == "off"
    assert fake.requests == []


def test_an_unknown_running_commit_is_reported_without_asking(running):
    running(commit=None)
    fake = use(Fake())
    status = updates.get_status(True)
    assert status.state == "unknown" and status.running_commit is None
    assert fake.requests == []


def test_the_same_commit_is_current(running):
    use(Fake(compare={"status": "identical", "ahead_by": 0, "behind_by": 0, "commits": []}))
    status = updates.get_status(True)
    assert status.state == "current"
    assert status.running_commit == RUNNING and status.running_version == "0.2.0"
    assert status.error is None


def test_newer_commits_are_counted_and_the_newest_described(running):
    use(Fake(compare=commits_ahead(3)))
    status = updates.get_status(True)
    assert status.state == "behind" and status.behind_by == 3
    assert status.latest.commit == LATEST
    assert status.latest.message == "Add the web client"  # the first line only
    assert status.latest.date == "2026-09-21T10:00:00Z"
    assert status.compare_url.startswith("https://github.com/")
    assert status.image is None  # not a container: nothing to say about an image


def test_a_build_ahead_of_main_or_diverged_is_told_apart(running):
    use(Fake(compare={"status": "behind", "ahead_by": 0, "behind_by": 2, "commits": []}))
    assert updates.get_status(True).state == "ahead"
    use(Fake(compare={**commits_ahead(1), "status": "diverged"}))
    status = updates.get_status(True)
    assert status.state == "diverged" and status.behind_by == 1


def test_a_commit_github_does_not_know_is_a_local_build(running):
    use(Fake(compare_status=404))
    assert updates.get_status(True).state == "unpublished"


def test_the_request_names_only_the_running_commit(running):
    fake = use(Fake(compare=commits_ahead()))
    updates.get_status(True)
    (request,) = fake.requests
    assert str(request.url) == f"https://api.github.com/repos/{REPO}/compare/{RUNNING}...main"
    assert request.headers["user-agent"].startswith("rustdesk-api-server/")
    assert not request.url.query and "authorization" not in request.headers and not request.content


@pytest.mark.parametrize(
    ("manifest", "token", "expected"),
    [(200, None, "published"), (404, None, "pending"), (500, None, "unknown"), (200, {}, "unknown")],
)
def test_a_container_that_is_behind_learns_whether_its_image_exists(running, manifest, token, expected):
    running(image=True)
    fake = use(Fake(compare=commits_ahead(), manifest=manifest, token=token))
    status = updates.get_status(True)
    assert status.state == "behind" and status.in_image
    assert status.image == expected and status.image_tag == f"sha-{LATEST[:7]}"
    if token != {}:
        head = fake.requests[-1]
        assert head.method == "HEAD" and str(head.url).endswith(f"/manifests/sha-{LATEST[:7]}")
        assert head.headers["authorization"] == "Bearer anonymous-pull-token"


def test_a_container_that_is_current_does_not_ask_ghcr(running):
    running(image=True)
    fake = use(Fake(compare={"status": "identical", "commits": []}))
    updates.get_status(True)
    assert [r.url.host for r in fake.requests] == ["api.github.com"]


def test_the_anonymous_limit_is_explained_and_retried_sooner(running, monkeypatch):
    fake = use(Fake(compare_status=403, compare_headers={"x-ratelimit-remaining": "0"}))
    status = updates.get_status(True)
    assert status.state == "error" and "limit" in status.error
    ttl = updates._cache[1]
    assert ttl == updates.FAILURE_TTL_SECONDS < updates.SUCCESS_TTL_SECONDS
    assert len(fake.requests) == 1


@pytest.mark.parametrize("failure", ["network", "server", "garbage"])
def test_failures_become_an_error_state_not_an_exception(running, failure):
    if failure == "network":

        def down(request):
            raise httpx.ConnectError("no route", request=request)

        updates.set_transport(httpx.MockTransport(down))
        expected = "could not be reached"
    elif failure == "server":
        use(Fake(compare_status=502))
        expected = "502"
    else:
        use(Fake(compare={"status": "something-new"}))
        expected = "unexpected"
    status = updates.get_status(True)
    assert status.state == "error" and expected in status.error


def test_the_answer_is_cached_and_a_forced_refresh_is_rate_limited(running, monkeypatch):
    fake = use(Fake(compare={"status": "identical", "commits": []}))
    clock = [1000.0]
    monkeypatch.setattr(updates.time, "monotonic", lambda: clock[0])
    updates.get_status(True)
    clock[0] += 3600
    updates.get_status(True)
    assert len(fake.requests) == 1  # within the hours it is kept
    updates.get_status(True, refresh=True)
    assert len(fake.requests) == 2  # asked for, and an hour old
    clock[0] += 10
    updates.get_status(True, refresh=True)
    assert len(fake.requests) == 2  # asked again 10 s later: not worth another request
    clock[0] += updates.SUCCESS_TTL_SECONDS
    updates.get_status(True)
    assert len(fake.requests) == 3  # expired


def test_the_repository_comes_from_the_projects_address():
    assert updates._repository() == REPO
