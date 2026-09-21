"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from rustdesk_api import __version__
from rustdesk_api.api import api_router
from rustdesk_api.api.deps import resolve_client_ip
from rustdesk_api.config import Settings, get_settings
from rustdesk_api.db.database import init_engine
from rustdesk_api.db.migrations.runner import run_migrations
from rustdesk_api.errors import ApiError
from rustdesk_api.maintenance import metrics_loop, retention_loop
from rustdesk_api.security import network as network_policy
from rustdesk_api.services.address_book import AddressBookError
from rustdesk_api.services.metrics import Counters
from rustdesk_api.services.server_metrics import MetricsSampler
from rustdesk_api.web.routes import web_router
from rustdesk_api.ws import DeviceConnectionManager

logger = logging.getLogger(__name__)

_CSP_COMMON = "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
# The WebUI keeps every script in /static/js (no inline <script>, no inline
# on*= handlers, no eval), so scripts may only come from this origin: an HTML
# injection can no longer run script, which is what makes the escaping in the
# templates a second line of defence rather than the only one. Styles are not
# restricted (tag colors are inline style attributes).
_WEBUI_CSP = f"script-src 'self'; script-src-attr 'none'; {_CSP_COMMON}"
# FastAPI's generated Swagger UI / ReDoc pages need an inline bootstrap script
# and load their bundles from a CDN, so they cannot satisfy the policy above.
# They are only served when API_DOCS_ENABLED is on.
_API_DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})
_API_DOCS_CSP = _CSP_COMMON


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)
    init_engine(settings)
    run_migrations(settings)
    logger.info("rustdesk-api starting up (version=%s)", __version__)
    sampler = MetricsSampler(
        settings.server_metrics_interval_seconds, settings.server_metrics_history_minutes
    )
    app.state.metrics_sampler = sampler
    background_tasks = [
        asyncio.create_task(retention_loop(settings), name="log-retention"),
        asyncio.create_task(metrics_loop(sampler), name="server-metrics"),
    ]
    try:
        yield
    finally:
        for task in background_tasks:
            task.cancel()
        for task in background_tasks:
            # The cancellation is the expected outcome, not an error to report.
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("rustdesk-api shutting down")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="RustDesk API Server",
        version=__version__,
        docs_url="/docs" if settings.api_docs_enabled else None,
        redoc_url="/redoc" if settings.api_docs_enabled else None,
        openapi_url="/openapi.json" if settings.api_docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.ws_device_manager = DeviceConnectionManager()
    app.state.counters = Counters()

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # RustDesk-protocol endpoints this server's compatibility has not yet
    # been verified for (see docs/rustdesk-compatibility.md).
    _rustdesk_compat_paths = {
        "/api/login",
        "/api/logout",
        "/api/currentUser",
        "/api/heartbeat",
        "/api/sysinfo",
        "/api/login-options",
        "/api/device-group/accessible",
        "/api/ab",
        "/api/ab/personal",
        "/api/users",
        "/api/peers",
        "/api/audit/conn",
        "/api/audit/conn/active",
        "/api/audit/file",
        "/api/audit",
    }
    # A connection note is text the user typed: log that it was sent, not what it says.
    _redacted_fields = {"password", "note"}

    def _is_rustdesk_compat_path(path: str) -> bool:
        # The newer address-book protocol has a book guid in most of its paths.
        return path in _rustdesk_compat_paths or path.startswith("/api/ab/")

    async def _log_rustdesk_compat_request(request: Request) -> None:
        """DEBUG-only, and only for the handful of RustDesk client-protocol
        paths above - explicitly for comparing real client traffic against
        docs/rustdesk-compatibility.md's documented assumptions. Never
        enabled by default (CLAUDE.md section 21: avoid logging full
        request bodies by default) and never logs Authorization or
        password values."""
        if not _is_rustdesk_compat_path(request.url.path):
            return
        body_bytes = await request.body()
        try:
            body = json.loads(body_bytes) if body_bytes else {}
        except ValueError:
            logger.debug("rustdesk-compat request body was not valid JSON (%d bytes)", len(body_bytes))
            return

        if request.url.path == "/api/ab" and request.method == "POST":
            # Address book pushes carry a client-computed `hash` per peer
            # (see AddressBookEntry.hash) that may be credential-adjacent
            # (CLAUDE.md section 18) - summarize instead of echoing it.
            try:
                inner = json.loads(body.get("data", "{}"))
                redacted = {"peer_count": len(inner.get("peers", []))}
            except (ValueError, TypeError):
                redacted = {"peer_count": "unparseable"}
        elif request.url.path.startswith("/api/ab/"):
            # Per-peer bodies carry `hash`/`password`, names and notes: log
            # only the shape.
            redacted = (
                {k: type(v).__name__ for k, v in body.items()}
                if isinstance(body, dict)
                else {"type": type(body).__name__, "len": len(body) if isinstance(body, list) else None}
            )
        elif request.url.path == "/api/audit/file":
            # File names/paths are user data - log only the shape, not the
            # values (the DEBUG capture exists to verify field names/types).
            redacted = {k: type(v).__name__ for k, v in body.items()}
        else:
            redacted = {k: ("***" if k in _redacted_fields else v) for k, v in body.items()}
        auth_present = "Authorization" in request.headers
        logger.debug(
            "rustdesk-compat %s %s?%s auth_header_present=%s body=%s",
            request.method,
            request.url.path,
            request.url.query,
            auth_present,
            redacted,
        )

    allowed_networks = settings.webui_allowed_network_list

    def _network_allowed(request: Request) -> bool:
        """WEBUI_ALLOWED_NETWORKS: only the WebUI and the management API are
        restricted (see security.network.is_restricted_path)."""
        if not network_policy.is_restricted_path(request.url.path):
            return True
        return network_policy.is_allowed(resolve_client_ip(request, settings), allowed_networks)

    @app.middleware("http")
    async def add_security_headers_and_request_id(request: Request, call_next):
        request_id = str(uuid.uuid4())
        if allowed_networks and not _network_allowed(request):
            logger.warning("Refused %s from a network outside WEBUI_ALLOWED_NETWORKS", request.url.path)
            if request.url.path.startswith("/api/"):
                blocked: Response = JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "NETWORK_NOT_ALLOWED",
                            "message": "This address is not allowed to use the management interface.",
                        }
                    },
                )
            else:
                blocked = Response("Forbidden", status_code=403, media_type="text/plain")
            blocked.headers["X-Request-ID"] = request_id
            blocked.headers["X-Content-Type-Options"] = "nosniff"
            return blocked
        if settings.log_level.upper() == "DEBUG":
            await _log_rustdesk_compat_request(request)
        start = time.monotonic()
        response: Response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            _API_DOCS_CSP if request.url.path in _API_DOCS_PATHS else _WEBUI_CSP
        )
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/static/"):
            # StaticFiles sends an ETag but no Cache-Control, so browsers apply
            # heuristic caching and keep serving an old app.js against newer
            # templates ("safeNextPath is not defined"). no-cache = always
            # revalidate; unchanged files still cost only a 304.
            response.headers["Cache-Control"] = "no-cache"
        logger.info(
            "%s %s -> %s (%.1fms) [%s]",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_id,
        )
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(AddressBookError)
    async def handle_address_book_error(_request: Request, exc: AddressBookError) -> JSONResponse:
        # Raised by the WebUI's address-book API; the RustDesk client protocol
        # catches these itself and answers in the client's own error shape.
        return JSONResponse(
            status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            content = detail
        else:
            content = {"error": {"code": "HTTP_ERROR", "message": str(detail)}}
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed.",
                    # A validator that raises ValueError leaves the exception itself in `ctx`.
                    "details": jsonable_encoder(exc.errors(), custom_encoder={Exception: str}),
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception while processing request")
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "An internal error occurred."}},
        )

    app.include_router(api_router)
    app.include_router(web_router)

    return app
