from __future__ import annotations

import json

from sqlalchemy.orm import Session

from rustdesk_api.models.audit import AuditLog


def record(
    db: Session,
    *,
    action: str,
    actor_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    result: str = "success",
    ip_address: str | None = None,
    detail: dict | None = None,
) -> AuditLog:
    """Append an audit log entry. Callers must never pass secrets in `detail`."""
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        result=result,
        ip_address=ip_address,
        detail=json.dumps(detail) if detail else None,
    )
    db.add(entry)
    db.flush()
    return entry
