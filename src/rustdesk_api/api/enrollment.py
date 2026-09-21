"""Device enrollment: `rustdesk --assign` and the tokens it uses.

* `POST /api/devices/cli` is the RustDesk client's own endpoint (`--assign`,
  `core_main.rs`). It answers with plain text: an empty 200 means done (the
  client prints "Done!"), anything else is printed as it is.
* `/api/v1/enrollment-tokens` (WebUI) manages the bearer tokens people pass to
  `--token`. An enrollment token can do that one thing and nothing else - every
  other endpoint ignores it (see services.tokens.get_valid_session).
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Body, Depends, Header
from fastapi.responses import PlainTextResponse, Response
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
from rustdesk_api.security.encryption import get_secret_box
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import enrollment as enrollment_service
from rustdesk_api.services import tokens as token_service

router = APIRouter(tags=["enrollment"])


def _text(status_code: int, message: str) -> PlainTextResponse:
    return PlainTextResponse(message, status_code=status_code)


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    return parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else None


@router.post("/api/devices/cli", dependencies=[Depends(enforce_auth_rate_limit)])
def assign_device(
    body: dict = Body(...),
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    client_ip: str | None = Depends(get_client_ip),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    """`rustdesk --assign --token <token> --user_name ... --address_book_name ...`"""
    raw_token = _bearer(authorization)
    session_obj = None
    if raw_token:
        session_obj = token_service.get_valid_session(
            db, raw_token, enrollment=True
        ) or token_service.get_valid_session(db, raw_token)
    if session_obj is None or not session_obj.user.is_active:
        return _text(401, "The token is invalid, expired or revoked.")
    actor = session_obj.user

    rustdesk_id, uuid = body.get("id"), body.get("uuid")
    if not isinstance(rustdesk_id, str) or not rustdesk_id.strip() or len(rustdesk_id) > 64:
        return _text(400, "The device id is missing.")
    rustdesk_id = rustdesk_id.strip()
    if uuid is not None and (not isinstance(uuid, str) or len(uuid) > 64):
        return _text(400, "The device uuid is invalid.")

    assignment = enrollment_service.assignment_from_body(body)
    if assignment.is_empty():
        return _text(400, "There is nothing to assign.")

    device = device_service.get_by_rustdesk_id(db, rustdesk_id)
    if device is not None and device.uuid and uuid and device.uuid != uuid:
        return _text(409, "That id is registered to a different machine.")
    if device is not None and device.approval == device_service.REJECTED:
        return _text(409, "That device was rejected by an administrator.")
    vouched = device_service.vouches(actor)
    if device is None:
        # The client normally has uploaded its system info by now, but the
        # command may run first (an installer script): register it, the
        # sysinfo upload fills in the rest.
        try:
            device = device_service.register_or_update(
                db,
                rustdesk_id=rustdesk_id,
                uuid=uuid,
                ip_address=client_ip,
                require_approval=settings.new_device_policy == "approve" and not vouched,
                vouched=vouched,
                pending_limit=settings.new_device_pending_limit,
            )
        except device_service.PendingDevicesFull:
            return _text(
                503, "Too many devices are waiting for approval. Ask an administrator to decide on them."
            )
    else:
        if device.uuid is None and uuid:
            device.uuid = uuid
        if vouched and device.approval == device_service.PENDING:
            # An administrator assigning a device is vouching for it.
            device_service.approve(db, device, actor_id=actor.id, via="assignment")

    try:
        enrollment_service.apply(
            db, device, assignment, actor=actor, box=get_secret_box(settings.data_encryption_key)
        )
    except enrollment_service.EnrollmentError as exc:
        db.rollback()
        return _text(400, str(exc))

    audit_service.record(
        db,
        action="device_assigned",
        actor_id=actor.id,
        target_type="device",
        target_id=device.id,
        ip_address=client_ip,
        detail={"rustdesk_id": device.rustdesk_id, "via": "rustdesk_cli", **assignment.redacted()},
    )
    db.commit()
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Enrollment tokens (WebUI)
# ---------------------------------------------------------------------------

MAX_TOKEN_DAYS = 365


class EnrollmentTokenOut(BaseModel):
    id: int
    label: str | None
    created_at: datetime.datetime
    expires_at: datetime.datetime
    last_used_at: datetime.datetime | None


class CreateEnrollmentTokenRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    days: int = Field(default=90, ge=1, le=MAX_TOKEN_DAYS)


class CreatedEnrollmentToken(EnrollmentTokenOut):
    # Shown once, here; only a hash is stored.
    token: str


def _token_out(session_obj: AuthSession) -> EnrollmentTokenOut:
    return EnrollmentTokenOut(
        id=session_obj.id,
        label=session_obj.label,
        created_at=session_obj.created_at,
        expires_at=session_obj.expires_at,
        last_used_at=session_obj.last_used_at,
    )


def _own_tokens(db: Session, user: User) -> list[AuthSession]:
    stmt = (
        select(AuthSession)
        .where(
            AuthSession.user_id == user.id,
            AuthSession.kind == token_service.ENROLLMENT_KIND,
            AuthSession.revoked_at.is_(None),
        )
        .order_by(AuthSession.created_at.desc())
    )
    return [s for s in db.execute(stmt).scalars() if s.is_valid()]


@router.get("/api/v1/enrollment-tokens", response_model=list[EnrollmentTokenOut])
def list_enrollment_tokens(
    db: Session = Depends(get_db), user: User = Depends(get_interactive_user)
) -> list[EnrollmentTokenOut]:
    return [_token_out(s) for s in _own_tokens(db, user)]


@router.post(
    "/api/v1/enrollment-tokens",
    response_model=CreatedEnrollmentToken,
    status_code=201,
    dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)],
)
def create_enrollment_token(
    payload: CreateEnrollmentTokenRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_interactive_user),
    client_ip: str | None = Depends(get_client_ip),
) -> CreatedEnrollmentToken:
    session_obj, raw_token = token_service.create_session(
        db,
        user=user,
        lifetime_seconds=payload.days * 86400,
        kind=token_service.ENROLLMENT_KIND,
        ip_address=client_ip,
        label=payload.label.strip(),
    )
    audit_service.record(
        db,
        action="enrollment_token_created",
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        ip_address=client_ip,
        detail={"label": session_obj.label, "days": payload.days},
    )
    db.commit()
    return CreatedEnrollmentToken(**_token_out(session_obj).model_dump(), token=raw_token)


@router.delete("/api/v1/enrollment-tokens/{token_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def revoke_enrollment_token(
    token_id: int, db: Session = Depends(get_db), user: User = Depends(get_interactive_user)
) -> None:
    # Own tokens only: another user's token id answers exactly like a missing one.
    session_obj = next((s for s in _own_tokens(db, user) if s.id == token_id), None)
    if session_obj is None:
        raise ApiError("TOKEN_NOT_FOUND", "The requested token does not exist.", 404)
    token_service.revoke_session(db, session_obj)
    audit_service.record(
        db,
        action="enrollment_token_revoked",
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        detail={"label": session_obj.label},
    )
    db.commit()
