"""The WebUI's stylesheet is compiled ahead of time and committed, so the app
loads nothing from third-party origins and needs no Node at runtime.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "src" / "rustdesk_api" / "web"
COMPILED_CSS = WEB_DIR / "static" / "css" / "tailwind.css"


def test_pages_load_no_third_party_assets():
    # Was <script src="https://cdn.tailwindcss.com">: a runtime dependency on
    # an external origin that also executes remote JS in every page.
    external = re.compile(r"""(?:src|href)\s*=\s*["']https?://""", re.IGNORECASE)
    for path in (WEB_DIR / "templates").glob("*.html"):
        assert not external.search(path.read_text(encoding="utf-8")), f"{path.name} loads a remote asset"


def test_base_template_applies_theme_before_the_stylesheet():
    # theme.js must be a synchronous script ahead of the CSS in <head>, or dark-mode
    # viewers see a flash of the light theme on every navigation.
    head = (WEB_DIR / "templates" / "base.html").read_text(encoding="utf-8")
    theme, css = head.index("/static/js/theme.js"), head.index("/static/css/tailwind.css")
    assert theme < css
    assert "defer" not in head[theme - 40 : theme + 60] and "async" not in head[theme - 40 : theme + 60]


def test_login_page_serves_local_assets(client):
    page = client.get("/login").text
    assert "/static/css/tailwind.css" in page and "/static/js/theme.js" in page
    for asset, media in (("/static/css/tailwind.css", "text/css"), ("/static/js/theme.js", "javascript")):
        r = client.get(asset)
        assert r.status_code == 200
        assert media in r.headers["content-type"]


def test_compiled_css_defines_both_themes_and_every_accent():
    # The minifier drops the quotes from attribute selectors, so ignore them.
    css = COMPILED_CSS.read_text(encoding="utf-8").replace('"', "")
    assert "data-theme=dark" in css
    for accent in ("blue", "indigo", "violet", "emerald", "rose", "amber"):
        assert f"data-accent={accent}]" in css


def test_compiled_css_is_up_to_date(tmp_path):
    """Fails when a template/app.js class was added without `npm run build:css`."""
    node = shutil.which("node")
    cli = ROOT / "node_modules" / "tailwindcss" / "lib" / "cli.js"
    if node is None or not cli.exists():
        pytest.skip("Node build tooling not installed (run `npm install`)")
    rebuilt = tmp_path / "tailwind.css"
    result = subprocess.run(
        [
            node,
            str(cli),
            "-c",
            str(ROOT / "tailwind.config.js"),
            "-i",
            str(ROOT / "frontend" / "tailwind.css"),
            "-o",
            str(rebuilt),
            "--minify",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert rebuilt.read_text(encoding="utf-8") == COMPILED_CSS.read_text(encoding="utf-8"), (
        "static/css/tailwind.css is stale - run `npm run build:css` and commit the result"
    )


def test_favicon_is_linked_by_every_page_and_served(client):
    head = (WEB_DIR / "templates" / "base.html").read_text(encoding="utf-8")
    for href in (
        "/favicon.ico",
        "/static/img/favicon-32.png",
        "/static/img/favicon-16.png",
        "/static/img/apple-touch-icon.png",
    ):
        assert f'href="{href}"' in head, href
        r = client.get(href)
        assert r.status_code == 200, href
        assert r.headers["content-type"].startswith("image/"), href
    # Not behind login: the browser requests it before (and without) a session.
    assert client.get("/favicon.ico").content[:4] == b"\x00\x00\x01\x00"  # ICO magic
    assert client.get("/static/img/apple-touch-icon.png").content[:8] == b"\x89PNG\r\n\x1a\n"
