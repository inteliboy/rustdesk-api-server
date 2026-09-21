from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_settings_dep
from rustdesk_api.buildinfo import get_build_info
from rustdesk_api.config import Settings
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
def version(settings: Settings = Depends(get_settings_dep)) -> dict:
    """`version` is what the endpoint always returned; the rest says which
    commit this is and which RustDesk client source the protocol was checked
    against (see rustdesk_api/buildinfo.py)."""
    return get_build_info(settings.git_commit).as_dict()
