"""GET /metrics for Prometheus. Off (404) unless METRICS_TOKEN is set; then it
needs `Authorization: Bearer <that token>`."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_settings_dep
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.security.tokens import constant_time_compare
from rustdesk_api.services import metrics as metrics_service

router = APIRouter(tags=["health"])


@router.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
def metrics(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    authorization: str | None = Header(default=None),
) -> PlainTextResponse:
    if not settings.metrics_token:
        raise ApiError("NOT_FOUND", "Not found.", 404)
    parts = (authorization or "").split(" ", 1)
    supplied = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""
    if not supplied or not constant_time_compare(supplied, settings.metrics_token):
        raise ApiError("NOT_AUTHENTICATED", "A valid metrics token is required.", 401)
    counters = getattr(request.app.state, "counters", None) or metrics_service.Counters()
    return PlainTextResponse(
        metrics_service.render(db, settings, counters),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
