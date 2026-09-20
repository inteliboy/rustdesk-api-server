"""Given logs of various ages, when retention runs, then only logs past their
own kind's limit are deleted."""

import datetime
import json

import pytest
from sqlalchemy import func, select

from rustdesk_api.db.database import get_session_factory
from rustdesk_api.models.audit import AuditLog
from rustdesk_api.models.client_audit import AlarmLog, ConnectionLog, FileTransferLog
from rustdesk_api.models.session import AuthSession
from rustdesk_api.services import authentication as auth_service
from rustdesk_api.services import retention
from rustdesk_api.services import tokens as token_service

NOW = datetime.datetime(2026, 9, 20, 12, 0, tzinfo=datetime.timezone.utc)


def _ago(days: float) -> datetime.datetime:
    return NOW - datetime.timedelta(days=days)


def _count(db, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar_one()


def _seed(db) -> None:
    for age, action in ((400, "old_a"), (366, "old_b"), (364, "new_a"), (1, "new_b")):
        db.add(AuditLog(action=action, created_at=_ago(age)))
    for age in (200, 181, 179, 1):
        db.add(ConnectionLog(rustdesk_id="1", started_at=_ago(age)))
        db.add(FileTransferLog(rustdesk_id="1", logged_at=_ago(age)))
        db.add(AlarmLog(rustdesk_id="1", logged_at=_ago(age)))
    db.commit()


def _settings(settings, *, audit=365, connection=180):
    return settings.model_copy(
        update={"audit_log_retention_days": audit, "connection_log_retention_days": connection}
    )


def test_each_kind_of_log_is_purged_by_its_own_limit(settings, app):
    with get_session_factory()() as db:
        _seed(db)
        result = retention.purge_expired(db, _settings(settings), now=NOW)

        assert (result.audit_logs, result.connection_logs, result.file_transfer_logs) == (2, 2, 2)
        assert result.alarm_logs == 2
        remaining = set(db.execute(select(AuditLog.action)).scalars())
        # The two recent entries survive, and so does the trace of the purge.
        assert remaining == {"new_a", "new_b", retention.AUDIT_ACTION}
        assert _count(db, ConnectionLog) == 2 and _count(db, FileTransferLog) == 2
        assert _count(db, AlarmLog) == 2


def test_zero_keeps_that_kind_forever(settings, app):
    with get_session_factory()() as db:
        _seed(db)
        result = retention.purge_expired(db, _settings(settings, audit=0, connection=0), now=NOW)

        assert result == retention.PurgeResult()
        assert (_count(db, AuditLog), _count(db, ConnectionLog), _count(db, FileTransferLog)) == (4, 4, 4)
        assert _count(db, AlarmLog) == 4


def test_disabling_one_kind_leaves_the_other_purged(settings, app):
    with get_session_factory()() as db:
        _seed(db)
        result = retention.purge_expired(db, _settings(settings, audit=0), now=NOW)

        assert result.audit_logs == 0 and result.connection_logs == 2
        assert _count(db, AuditLog) == 4 + 1  # nothing purged, plus the purge's own entry


def test_purge_leaves_an_audit_trace_with_counts_and_no_secrets(settings, app):
    with get_session_factory()() as db:
        _seed(db)
        retention.purge_expired(db, _settings(settings), now=NOW)

        entry = db.execute(select(AuditLog).where(AuditLog.action == retention.AUDIT_ACTION)).scalar_one()
        assert entry.actor_id is None
        assert json.loads(entry.detail) == {
            "audit_logs": 2,
            "connection_logs": 2,
            "file_transfer_logs": 2,
            "alarm_logs": 2,
            "audit_log_retention_days": 365,
            "connection_log_retention_days": 180,
        }


def test_a_run_that_deletes_no_log_records_nothing(settings, app):
    """Otherwise every scheduled run would itself grow the audit log."""
    with get_session_factory()() as db:
        result = retention.purge_expired(db, _settings(settings), now=NOW)
        assert result == retention.PurgeResult()
        assert _count(db, AuditLog) == 0


def test_dry_run_reports_but_changes_nothing(settings, app):
    with get_session_factory()() as db:
        _seed(db)
        result = retention.purge_expired(db, _settings(settings), now=NOW, dry_run=True)

        assert (result.audit_logs, result.connection_logs, result.file_transfer_logs) == (2, 2, 2)
        assert (_count(db, AuditLog), _count(db, ConnectionLog), _count(db, FileTransferLog)) == (4, 4, 4)
        assert _count(db, AlarmLog) == 4


def test_deletes_in_batches_and_gets_everything(settings, app, monkeypatch):
    monkeypatch.setattr(retention, "BATCH_SIZE", 2)
    with get_session_factory()() as db:
        for i in range(7):  # 3 full batches and a partial one
            db.add(AuditLog(action=f"old{i}", created_at=_ago(500 + i)))
        db.add(AuditLog(action="keep", created_at=_ago(1)))
        db.commit()

        result = retention.purge_expired(db, _settings(settings), now=NOW)

        assert result.audit_logs == 7
        assert set(db.execute(select(AuditLog.action)).scalars()) == {"keep", retention.AUDIT_ACTION}


def test_a_batch_boundary_exactly_at_the_row_count_terminates(settings, app, monkeypatch):
    monkeypatch.setattr(retention, "BATCH_SIZE", 2)
    with get_session_factory()() as db:
        for i in range(4):
            db.add(AuditLog(action=f"old{i}", created_at=_ago(500)))
        db.commit()
        assert retention.purge_expired(db, _settings(settings), now=NOW).audit_logs == 4


def test_a_log_exactly_at_the_limit_is_kept(settings, app):
    with get_session_factory()() as db:
        db.add(AuditLog(action="edge", created_at=_ago(365)))
        db.commit()
        assert retention.purge_expired(db, _settings(settings), now=NOW).audit_logs == 0


def test_expired_sessions_are_removed_and_live_ones_kept(settings, app):
    Session = get_session_factory()
    with Session() as db:
        user = auth_service.create_user(db, username="alice", password="passwordpassword1")
        live, _ = token_service.create_session(db, user=user, lifetime_seconds=3600)
        dead, _ = token_service.create_session(db, user=user, lifetime_seconds=3600)
        dead.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
        db.commit()
        live_id = live.id

        result = retention.purge_expired(db, _settings(settings, audit=0, connection=0))

        assert result.sessions == 1
        assert [s.id for s in db.execute(select(AuthSession)).scalars()] == [live_id]


@pytest.mark.parametrize("name", ["AUDIT_LOG_RETENTION_DAYS", "CONNECTION_LOG_RETENTION_DAYS"])
def test_negative_retention_is_rejected_at_startup(monkeypatch, name):
    from pydantic import ValidationError

    from rustdesk_api.config import Settings

    monkeypatch.setenv(name, "-1")
    with pytest.raises(ValidationError):
        Settings()
