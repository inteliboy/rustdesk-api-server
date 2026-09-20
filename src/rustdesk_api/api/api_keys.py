"""Personal API keys (/api/v1/api-keys): long-lived bearer tokens for scripts
that call the management API (/api/v1).

A key acts as its owner, so it can do everything the owner can - unless it is
"read" scoped, which limits it to GET requests. It works only as an
`Authorization: Bearer` header on /api/v1 (never as a cookie and never on the
RustDesk client endpoints), and it cannot manage the account itself: create
keys, change two-factor, list sessions (see api.deps.get_interactive_user).
Only a hash is stored, so the token is shown once, at creation.
"""

from __future__ import annotations

import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import (
    enforce_auth_rate_limit,
    get_client_ip,
    get_interactive_user,
    get_settings_dep,
    verify_csrf,
)
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.session import AuthSession
from rustdesk_api.models.user import User
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import tokens as token_service

router = APIRouter(prefix="/api/v1/api-keys", tags=["api-keys"])

MAX_KEY_DAYS = 365
MAX_KEYS_PER_USER = 25


class ApiKeyOut(BaseModel):
    id: int
    label: str | None
    scope: str
    created_at: datetime.datetime
    expires_at: datetime.datetime
    last_used_at: datetime.datetime | None


class CreateApiKeyRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    days: int = Field(default=90, ge=1, le=MAX_KEY_DAYS)
    scope: Literal["read", "full"] = "read"


class CreatedApiKey(ApiKeyOut):
    # Shown once, here.
    token: str


def _to_out(row: AuthSession) -> ApiKeyOut:
    return ApiKeyOut(
        id=row.id,
        label=row.label,
        scope=row.scope,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
    )


def _own_keys(db: Session, user: User) -> list[AuthSession]:
    rows = db.execute(
        select(AuthSession)
        .where(
            AuthSession.user_id == user.id,
            AuthSession.kind == token_service.API_KEY_KIND,
            AuthSession.revoked_at.is_(None),
        )
        .order_by(AuthSession.created_at.desc())
    ).scalars()
    return [r for r in rows if r.is_valid()]


@router.get("", response_model=list[ApiKeyOut])
def list_keys(db: Session = Depends(get_db), user: User = Depends(get_interactive_user)) -> list[ApiKeyOut]:
    return [_to_out(r) for r in _own_keys(db, user)]


@router.post(
    "",
    response_model=CreatedApiKey,
    status_code=201,
    dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)],
)
def create_key(
    payload: CreateApiKeyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_interactive_user),
    client_ip: str | None = Depends(get_client_ip),
    settings: Settings = Depends(get_settings_dep),
) -> CreatedApiKey:
    if len(_own_keys(db, user)) >= MAX_KEYS_PER_USER:
        raise ApiError("TOO_MANY_API_KEYS", f"At most {MAX_KEYS_PER_USER} API keys per user.", 409)
    row, raw = token_service.create_session(
        db,
        user=user,
        lifetime_seconds=payload.days * 86400,
        kind=token_service.API_KEY_KIND,
        ip_address=client_ip,
        label=payload.label.strip(),
        scope=payload.scope,
    )
    audit_service.record(
        db,
        action="api_key_created",
        actor_id=user.id,
        target_type="api_key",
        target_id=row.id,
        ip_address=client_ip,
        detail={"label": row.label, "scope": row.scope, "days": payload.days},
    )
    db.commit()
    return CreatedApiKey(**_to_out(row).model_dump(), token=raw)


@router.delete("/{key_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def revoke_key(
    key_id: int, db: Session = Depends(get_db), user: User = Depends(get_interactive_user)
) -> None:
    # Own keys only; someone else's id looks the same as one that does not exist.
    row = next((r for r in _own_keys(db, user) if r.id == key_id), None)
    if row is None:
        raise ApiError("API_KEY_NOT_FOUND", "The requested API key does not exist.", 404)
    token_service.revoke_session(db, row)
    audit_service.record(
        db,
        action="api_key_revoked",
        actor_id=user.id,
        target_type="api_key",
        target_id=row.id,
        detail={"label": row.label},
    )
    db.commit()
