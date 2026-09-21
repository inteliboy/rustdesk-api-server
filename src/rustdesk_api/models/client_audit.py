from __future__ import annotations

import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from rustdesk_api.db.database import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class ConnectionLog(Base):
    """One remote-control session, as reported by the *controlled* RustDesk
    client via POST /api/audit/conn (see docs/rustdesk-compatibility.md).

    Everything here is client-reported and unauthenticated on the wire (the
    client sends no Authorization header for audit calls), so it is display
    data only - never used for an authorization decision.
    """

    __tablename__ = "connection_logs"
    __table_args__ = (Index("ix_connection_logs_rustdesk_conn", "rustdesk_id", "conn_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    # Nullable + SET NULL so history survives deleting the device; such
    # orphaned rows are then visible to administrators only.
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The controlled device's RustDesk id (the `id` field in the payload).
    rustdesk_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Per-hbbs-process counter on the client side, so it is only unique
    # among currently-open sessions of one device - never a global key.
    conn_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    from_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The controlling side, from the payload's `peer: [id, name]` tuple.
    peer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    peer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Raw client enum values, stored as sent. See the WebUI/docs for which
    # numeric values are verified against the client source.
    conn_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    primary_auth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    two_factor: Mapped[int | None] = mapped_column(Integer, nullable=True)

    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Client-generated per-request nonce; makes the "new" event idempotent
    # when the client retries after a timeout/5xx (CLAUDE.md section 43).
    nonce: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)

    # Handed to the controlling client that asks for it (GET /api/audit/conn/active)
    # so it can attach a note to this session afterwards (PUT /api/audit). Made on
    # first request, so a session nobody asks about never gets one.
    guid: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True, index=True)
    # What the controlling user typed in the client's end-of-session dialog.
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class FileTransferLog(Base):
    """A file-transfer event reported via POST /api/audit/file."""

    __tablename__ = "file_transfer_logs"

    id: Mapped[int] = mapped_column(primary_key=True)

    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    rustdesk_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    conn_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    peer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    peer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    from_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Raw FileAuditType value. The enum's numeric meaning could not be
    # verified against the client source, so it is stored and shown as-is.
    audit_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_file: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # JSON text: list of [name, size_bytes], capped (see services/client_audit.py).
    files: Mapped[str | None] = mapped_column(Text, nullable=True)

    logged_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    nonce: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)


class AlarmLog(Base):
    """A security alarm reported via POST /api/audit/alarm: the controlled
    client refused a connection attempt (IP/ID whitelist, too many failed
    logins, ...). Same trust level as the other client-reported logs - display
    data only, never used for an authorization decision."""

    __tablename__ = "alarm_logs"

    id: Mapped[int] = mapped_column(primary_key=True)

    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    rustdesk_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # The client's AlarmAuditType value (`typ`), stored as sent so a value this
    # server has no label for is still kept.
    alarm_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conn_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Who was refused, from the payload's `info`: the address it came from and,
    # for most alarm types, the connecting peer's id and name.
    from_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    peer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    peer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Only sent for a session-scope violation.
    conn_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str | None] = mapped_column(String(255), nullable=True)

    logged_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    nonce: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
