"""WebUI / browser-facing security: output escaping, tag colors, headers.

Stored XSS was the main risk here: hostnames, aliases, peer/file names and
tag names all arrive from clients or ordinary users and are rendered by
client-side JS via innerHTML (CLAUDE.md section 23, 65).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parents[2] / "src" / "rustdesk_api" / "web"
REPO_TESTS_WEB = Path(__file__).resolve().parents[1] / "web"

# Fields whose values come from clients or ordinary users. Any `${...}` that
# reads one of these must go through escapeHtml()/safeColor()/encodeURIComponent().
UNTRUSTED_FIELDS = (
    "alias|hostname|username|email|platform|os_version|client_version|ip_address|cpu|memory|name|"
    "description|owner_username|group_name|shared_with_username|shared_with|permission|peer_name|"
    "peer_id|path|rustdesk_id|from_ip|device_label|message|actor_username|color|audit_type|target_type|note|owner"
)
FIELD_ACCESS = re.compile(rf"\b(?!safe\b)\w+\.(?:{UNTRUSTED_FIELDS})\b")
# userLink() escapes the username and URI-encodes the id itself (app.js).
SANITIZERS = ("escapeHtml(", "safeColor(", "encodeURIComponent(", "userLink(", "connectLink(", "ipLabel(")


def _innermost_interpolations(text: str):
    """Yields the source of each innermost `${...}` (no nested `${` inside)."""
    stack: list[int] = []
    i = 0
    while i < len(text):
        if text.startswith("${", i):
            stack.append(i + 2)
            i += 2
            continue
        if text[i] == "{" and stack:
            stack.append(-1)  # plain brace inside an expression
        elif text[i] == "}" and stack:
            start = stack.pop()
            if start != -1:
                expr = text[start:i]
                if "${" not in expr:
                    yield expr
        i += 1


def test_no_untrusted_field_is_interpolated_into_html_unescaped():
    """A cheap tripwire, not a proof: it fails if a template/JS file reads a
    client- or user-controlled field inside `${...}` without a sanitizer, the
    exact shape of every XSS this UI had."""
    files = [
        *(WEB_DIR / "templates").glob("*.html"),
        WEB_DIR / "static" / "js" / "app.js",
        *(WEB_DIR / "static" / "js" / "pages").glob("*.js"),
    ]
    assert len(files) > 12  # the page scripts really are covered
    offenders = []
    for path in files:
        for expr in _innermost_interpolations(path.read_text(encoding="utf-8")):
            if FIELD_ACCESS.search(expr) and not any(s in expr for s in SANITIZERS):
                offenders.append(f"{path.name}: ${{{expr.strip()}}}")
    assert not offenders, "Unescaped untrusted fields:\n" + "\n".join(offenders)


def test_tripwire_actually_catches_a_regression():
    bad = "`<td>${d.hostname}</td>`"
    assert any(FIELD_ACCESS.search(e) for e in _innermost_interpolations(bad))
    good = "`<td>${escapeHtml(d.hostname)}</td>`"
    assert not any(
        FIELD_ACCESS.search(e) and not any(s in e for s in SANITIZERS)
        for e in _innermost_interpolations(good)
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_web_helpers_escape_and_validate_real_inputs():
    """Runs the shipped app.js helpers (escapeHtml, safeColor, safeNextPath,
    tagBadges, describeActivity) against XSS payloads in Node."""
    result = subprocess.run(
        ["node", str(REPO_TESTS_WEB / "helpers_check.js"), str(WEB_DIR / "static" / "js" / "app.js")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_responses_carry_baseline_security_headers(client):
    r = client.get("/health")
    csp = r.headers["content-security-policy"]
    for directive in ("object-src 'none'", "base-uri 'self'", "form-action 'self'", "frame-ancestors 'none'"):
        assert directive in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_static_assets_always_revalidate(client):
    # Without Cache-Control the browser heuristically caches app.js and runs an
    # old copy against newer templates (e.g. "safeNextPath is not defined").
    r = client.get("/static/js/app.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert "etag" in r.headers  # revalidation stays a cheap 304
    assert "cache-control" not in client.get("/health").headers


@pytest.mark.parametrize(
    "color",
    ["red", "#fff", "#12345", "#gggggg", 'red" onmouseover="x', "#ff0000;background:url(x)", ""],
)
def test_tag_api_rejects_non_hex_colors(admin_client, color):
    assert admin_client.post("/api/v1/tags", json={"name": "t", "color": color}).status_code == 422
    tag = admin_client.post("/api/v1/tags", json={"name": "ok", "color": "#ff0000"}).json()
    assert admin_client.patch(f"/api/v1/tags/{tag['id']}", json={"color": color}).status_code == 422


def test_tag_api_accepts_hex_colors(admin_client):
    r = admin_client.post("/api/v1/tags", json={"name": "t", "color": "#aBc123"})
    assert r.status_code == 201 and r.json()["color"] == "#aBc123"


def test_client_pushed_tag_colors_of_any_json_type_cannot_break_the_address_book(admin_client):
    """Real clients push tag colors as they like (the protocol's own format
    is an ARGB integer). Storage must not error whatever JSON type arrives, and
    the management API only ever hands the WebUI a #rrggbb color."""
    token = admin_client.post("/api/login", json={"username": "admin", "password": "adminpass123"}).json()[
        "access_token"
    ]
    colors = {"a": 4278190080, "b": {"nested": "x"}, "c": 'red" onmouseover="alert(1)' + "x" * 100}
    data = {
        "tags": list(colors),
        "peers": [{"id": "p1", "alias": "x", "tags": list(colors)}],
        "tag_colors": json.dumps(colors),
    }
    r = admin_client.post(
        "/api/ab", json={"data": json.dumps(data)}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200

    entry = admin_client.get("/api/v1/address-book").json()[0]
    assert {t["name"] for t in entry["tags"]} == {"a", "b", "c"}
    assert all(re.fullmatch(r"#[0-9a-f]{6}", t["color"]) for t in entry["tags"])
    # 4278190080 is opaque black (0xFF000000).
    assert next(t["color"] for t in entry["tags"] if t["name"] == "a") == "#000000"


# --- Content-Security-Policy: scripts only from this origin -------------------

WEB_PAGES = ["/", "/login", "/setup", "/dashboard", "/devices", "/devices/1", "/groups", "/tags"]
WEB_PAGES += ["/users", "/address-book", "/logs", "/strategies", "/security", "/register", "/reset-password"]
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>", re.IGNORECASE)
INLINE_HANDLER = re.compile(r"""<[^>]+\son[a-z]+\s*=""", re.IGNORECASE)
CSP_SOURCE_NEEDING_RELAXATION = ("'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'", "data:", "*", "http:")


def _directive(csp: str, name: str) -> str:
    return next(d.strip() for d in csp.split(";") if d.strip().startswith(name + " "))


@pytest.mark.parametrize("path", WEB_PAGES)
def test_every_web_page_is_served_with_a_strict_script_policy(client, path):
    r = client.get(path)
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert _directive(csp, "script-src") == "script-src 'self'"
    assert _directive(csp, "script-src-attr") == "script-src-attr 'none'"
    for weakening in CSP_SOURCE_NEEDING_RELAXATION:
        assert weakening not in csp.split("object-src")[0]


@pytest.mark.parametrize("path", WEB_PAGES)
def test_web_pages_contain_no_inline_script_or_handlers(client, path):
    """What the policy above forbids, so the page would silently break."""
    html = client.get(path).text
    assert not INLINE_SCRIPT.findall(html), "inline <script> would be blocked by the CSP"
    assert not INLINE_HANDLER.findall(html), "inline on*= handler would be blocked by the CSP"
    assert "javascript:" not in html.lower()


def test_templates_and_scripts_stay_csp_clean_even_for_pages_needing_data():
    """Covers templates no route test reaches (e.g. markup built in JS)."""
    for path in (WEB_DIR / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert not INLINE_SCRIPT.findall(text), path.name
        assert not INLINE_HANDLER.findall(text), path.name
    for path in (WEB_DIR / "static" / "js").rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        assert not INLINE_HANDLER.findall(text), f"{path.name} builds an inline on*= handler"
        assert not re.search(r"\beval\(|new Function\(|set(?:Timeout|Interval)\(\s*[\"'`]", text), path.name


def test_the_device_page_gets_its_id_from_a_data_attribute_not_inline_script(client):
    html = client.get("/devices/42").text
    assert 'id="device-page" data-device-id="42"' in html
    assert "/static/js/pages/device_detail.js" in html
    assert client.get("/devices/not-a-number").status_code == 422


def test_every_page_script_the_templates_reference_is_served(client):
    for path in (WEB_DIR / "templates").glob("*.html"):
        for src in re.findall(r'<script src="(/static/[^"]+)"', path.read_text(encoding="utf-8")):
            # The language catalog is chosen per request ({{ lang }}); tests/integration/test_web_language.py
            # fetches each one.
            src = src.replace("{{ lang }}", "pl")
            r = client.get(src)
            assert r.status_code == 200 and "javascript" in r.headers["content-type"], src


def test_the_generated_api_docs_keep_a_policy_they_can_run_under(client):
    """Swagger UI/ReDoc use an inline bootstrap script and a CDN; the strict
    policy would blank them, so they get the framing/plugin directives only."""
    for path in ("/docs", "/redoc"):
        r = client.get(path)
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        assert "script-src" not in csp
        assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    # ...but they must not leak the relaxation to anything else, JSON included.
    assert "script-src 'self'" in client.get("/openapi.json").headers["content-security-policy"]
    assert "script-src 'self'" in client.get("/api/version").headers["content-security-policy"]
