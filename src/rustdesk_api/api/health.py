from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from rustdesk_api import __version__
from rustdesk_api.db.database import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict:
    # A readiness probe must report failure rather than raise, so the
    # orchestrator sees a clean "not ready" instead of a 500.
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return {"status": "error", "database": "unavailable"}
    return {"status": "ok", "database": "ok"}


@router.get("/api/version")
def version() -> dict:
    return {"version": __version__}
