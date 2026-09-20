"""RustDesk client audit endpoints: POST /api/audit/conn, /api/audit/file and
/api/audit/alarm.

Paths and payload shapes taken from the real client source
(`get_audit_server` in src/common.rs, `post_conn_audit` / `post_file_audit` /
`post_alarm_audit` in src/server/connection.rs) - see
docs/rustdesk-compatibility.md. The client sends *no* Authorization header on
these calls, so they are unauthenticated by necessity; see
services/client_audit.py for how that is contained (known-device + uuid check,
rate limit, length caps).

Always answers 200 with an EMPTY body for well-formed requests, including ones
that are dropped, so the endpoints cannot be used to probe which device ids
exist. Empty is what newer clients read as "stored": they retry any other 2xx
body (`post_audit_async` in connection.rs), while 1.4.9 ignores the response.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import enforce_client_audit_rate_limit
from rustdesk_api.db.database import get_db
from rustdesk_api.services import client_audit as audit_service

router = APIRouter(tags=["rustdesk-compat"], dependencies=[Depends(enforce_client_audit_rate_limit)])


class ConnAuditRequest(BaseModel):
    id: str | None = None
    uuid: str | None = None
    conn_id: int | str | None = None
    session_id: int | str | None = None
    nonce: str | None = None
    action: str | None = None
    ip: str | None = None
    # [controlling peer id, controlling peer name] on authorization.
    peer: Any = None
    conn_type: Any = Field(default=None, alias="type")
    primary_auth: Any = None
    two_factor: Any = None


class FileAuditRequest(BaseModel):
    id: str | None = None
    uuid: str | None = None
    peer_id: str | None = None
    conn_id: int | str | None = None
    nonce: str | None = None
    audit_type: Any = Field(default=None, alias="type")
    path: str | None = None
    is_file: Any = None
    # A JSON-encoded string (same quirk as /api/ab's `data`).
    info: Any = None


class AlarmAuditRequest(BaseModel):
    id: str | None = None
    uuid: str | None = None
    # AlarmAuditType; see the Logs page for the labels.
    alarm_type: Any = Field(default=None, alias="typ")
    conn_id: int | str | None = None
    nonce: str | None = None
    # A JSON-encoded string (same quirk as /api/ab's `data`). The client may
    # also send `conn_audit_ref`, which is ignored (unknown keys are dropped).
    info: Any = None


@router.post("/api/audit/conn", response_class=Response)
def audit_connection(payload: ConnAuditRequest, db: Session = Depends(get_db)) -> Response:
    if not payload.id:
        return JSONResponse({"error": "Missing device id."})
    device = audit_service.resolve_reporting_device(db, payload.id, payload.uuid)
    if device is not None:
        audit_service.record_connection_event(
            db,
            device=device,
            conn_id=None if payload.conn_id is None else str(payload.conn_id),
            session_id=None if payload.session_id is None else str(payload.session_id),
            nonce=payload.nonce,
            action=payload.action,
            ip=payload.ip,
            peer=payload.peer,
            conn_type=payload.conn_type,
            primary_auth=payload.primary_auth,
            two_factor=payload.two_factor,
        )
        db.commit()
    return Response(status_code=200)


@router.post("/api/audit/file", response_class=Response)
def audit_file_transfer(payload: FileAuditRequest, db: Session = Depends(get_db)) -> Response:
    if not payload.id:
        return JSONResponse({"error": "Missing device id."})
    device = audit_service.resolve_reporting_device(db, payload.id, payload.uuid)
    if device is not None:
        audit_service.record_file_event(
            db,
            device=device,
            conn_id=None if payload.conn_id is None else str(payload.conn_id),
            peer_id=payload.peer_id,
            nonce=payload.nonce,
            audit_type=payload.audit_type,
            path=payload.path,
            is_file=payload.is_file,
            info=payload.info,
        )
        db.commit()
    return Response(status_code=200)


@router.post("/api/audit/alarm", response_class=Response)
def audit_alarm(payload: AlarmAuditRequest, db: Session = Depends(get_db)) -> Response:
    if not payload.id:
        return JSONResponse({"error": "Missing device id."})
    device = audit_service.resolve_reporting_device(db, payload.id, payload.uuid)
    if device is not None:
        audit_service.record_alarm_event(
            db,
            device=device,
            alarm_type=payload.alarm_type,
            conn_id=None if payload.conn_id is None else str(payload.conn_id),
            nonce=payload.nonce,
            info=payload.info,
        )
        db.commit()
    return Response(status_code=200)
