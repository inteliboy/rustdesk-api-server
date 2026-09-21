"""Server-rendered WebUI shell.

Pages are thin Jinja templates that fetch their data from the JSON API
(/api/v1/...) client-side. This keeps Phase 1 free of a Node.js build
toolchain (CLAUDE.md section 35) while still giving a real, testable HTML
entry point. Authorization is never enforced here - only the API layer
enforces it (CLAUDE.md section 66); these routes just serve markup.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from rustdesk_api import __version__

WEB_DIR = Path(__file__).resolve().parent

# The WebUI's languages: English is the source text, the others are catalogs in
# static/i18n (built from i18n/catalog.tsv by scripts/build_i18n.py).
SUPPORTED_LANGUAGES = ("en", "pl", "fr", "de", "es")
LANGUAGE_COOKIE = "rd_lang"


def pick_language(request: Request) -> str:
    """The language the person chose (cookie), else the first one their browser asks
    for that we have, else English."""
    chosen = request.cookies.get(LANGUAGE_COOKIE)
    if chosen in SUPPORTED_LANGUAGES:
        return chosen
    for part in request.headers.get("accept-language", "").split(","):
        code = part.split(";")[0].strip().lower().split("-")[0]
        if code in SUPPORTED_LANGUAGES:
            return code
    return "en"


def _language_context(request: Request) -> dict[str, str]:
    return {"lang": pick_language(request)}


templates = Jinja2Templates(directory=str(WEB_DIR / "templates"), context_processors=[_language_context])

web_router = APIRouter(include_in_schema=False)
web_router.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


@web_router.get("/favicon.ico")
def favicon() -> FileResponse:
    """Browsers ask for this path on their own (Safari, feed readers, direct
    visits to a URL that is not an HTML page), whatever the page's <link> says."""
    return FileResponse(WEB_DIR / "static" / "img" / "favicon.ico", media_type="image/x-icon")


@web_router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {})


@web_router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {})


@web_router.get("/register", response_class=HTMLResponse)
def register_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "register.html", {})


@web_router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "reset_password.html", {})


@web_router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "setup.html", {})


@web_router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html", {"active": "dashboard"})


@web_router.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "devices.html", {"active": "devices"})


@web_router.get("/devices/{device_id}", response_class=HTMLResponse)
def device_detail_page(request: Request, device_id: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "device_detail.html", {"active": "devices", "device_id": device_id}
    )


@web_router.get("/groups", response_class=HTMLResponse)
def groups_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "groups.html", {"active": "groups"})


@web_router.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "tags.html", {"active": "tags"})


@web_router.get("/users", response_class=HTMLResponse)
def users_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "users.html", {"active": "users"})


@web_router.get("/address-book", response_class=HTMLResponse)
def address_book_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "address_book.html", {"active": "address-book"})


@web_router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "logs.html", {"active": "logs"})


@web_router.get("/strategies", response_class=HTMLResponse)
def strategies_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "strategies.html", {"active": "strategies"})


@web_router.get("/connect", response_class=HTMLResponse)
def connect_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "connect.html", {"active": "connect"})


@web_router.get("/webclient", response_class=HTMLResponse)
def webclient_page(request: Request) -> HTMLResponse:
    """The browser web client for one device (?device=<id>). Full screen, so not
    inside the WebUI's shell; the API decides whether this user may start it."""
    return templates.TemplateResponse(request, "webclient.html", {"version": __version__})


@web_router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "settings.html", {"active": "settings"})


@web_router.get("/security", response_class=HTMLResponse)
def security_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "security.html", {"active": "security"})
