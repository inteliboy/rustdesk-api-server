"""Log retention: deleting audit and client-reported logs past their age limit.

Without this every login, admin action and RustDesk session/file-transfer
report stays in the database forever, and the unauthenticated /api/audit/*
endpoints let the fleet (or anyone able to guess a device id) grow it without
bound. Retention is configured per kind of log (see Settings); 0 keeps that
kind forever. Client-reported logs (connections, file transfers, alarms) share
one limit.
"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from rustdesk_api.config import Settings
from rustdesk_api.db.database import Base
from rustdesk_api.models.audit import AuditLog
from rustdesk_api.models.client_audit import AlarmLog, ConnectionLog, FileTransferLog
from rustdesk_api.models.device_event import DeviceEvent
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import oidc as oidc_service
from rustdesk_api.services import password_reset as reset_service
from rustdesk_api.services import tokens as token_service

# Deleting in bounded batches, committing between them, keeps the SQLite write
# lock short so a large first purge does not stall heartbeats and logins.
BATCH_SIZE = 1000

AUDIT_ACTION = "log_retention_purge"


@dataclass(frozen=True)
class PurgeResult:
    audit_logs: int = 0
    connection_logs: int = 0
    file_transfer_logs: int = 0
    alarm_logs: int = 0
    sessions: int = 0

    @property
    def logs_total(self) -> int:
        return self.audit_logs + self.connection_logs + self.file_transfer_logs + self.alarm_logs


def _cutoff(days: int, now: datetime.datetime) -> datetime.datetime | None:
    return None if days <= 0 else now - datetime.timedelta(days=days)


def _purge_older_than(
    db: Session, model: type[Base], column: InstrumentedAttribute, cutoff: datetime.datetime | None
) -> int:
    if cutoff is None:
        return 0
    total = 0
    while True:
        oldest = select(model.id).where(column < cutoff).limit(BATCH_SIZE)  # type: ignore[attr-defined]
        result: CursorResult = db.execute(delete(model).where(model.id.in_(oldest)))  # type: ignore[attr-defined,assignment]
        db.commit()
        total += result.rowcount
        if result.rowcount < BATCH_SIZE:
            return total


def _count_older_than(
    db: Session, model: type[Base], column: InstrumentedAttribute, cutoff: datetime.datetime | None
) -> int:
    if cutoff is None:
        return 0
    return db.execute(select(func.count()).select_from(model).where(column < cutoff)).scalar_one()


def purge_expired(
    db: Session, settings: Settings, *, now: datetime.datetime | None = None, dry_run: bool = False
) -> PurgeResult:
    """Deletes logs older than the configured retention and returns the counts.

    With `dry_run` nothing is deleted or recorded; the counts are what a real
    run would remove (sessions are not counted: they are not a log). A real run
    that removed any log adds one audit entry - deleting audit history should
    itself leave a trace - written after the deletion so it is never its own
    victim. Commits as it goes (per batch), so pass a session with no
    unrelated pending changes.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    audit_cutoff = _cutoff(settings.audit_log_retention_days, now)
    connection_cutoff = _cutoff(settings.connection_log_retention_days, now)
    work = (
        (AuditLog, AuditLog.created_at, audit_cutoff),
        (ConnectionLog, ConnectionLog.started_at, connection_cutoff),
        (FileTransferLog, FileTransferLog.logged_at, connection_cutoff),
        (AlarmLog, AlarmLog.logged_at, connection_cutoff),
    )

    if dry_run:
        counts = [_count_older_than(db, model, column, cutoff) for model, column, cutoff in work]
        return PurgeResult(*counts)

    counts = [_purge_older_than(db, model, column, cutoff) for model, column, cutoff in work]
    # Device timeline events age out with the audit log (they are not counted in
    # the result: they are neither an audit nor a client-reported log), and spent
    # or expired password-reset links go with the sessions.
    _purge_older_than(db, DeviceEvent, DeviceEvent.created_at, audit_cutoff)
    reset_service.purge_expired(db)
    oidc_service.purge_expired(db)
    sessions = token_service.cleanup_expired(db)
    db.commit()
    result = PurgeResult(*counts, sessions=sessions)

    if result.logs_total:
        audit_service.record(
            db,
            action=AUDIT_ACTION,
            detail={
                **{k: v for k, v in asdict(result).items() if k != "sessions"},
                "audit_log_retention_days": settings.audit_log_retention_days,
                "connection_log_retention_days": settings.connection_log_retention_days,
            },
        )
        db.commit()
    return result
