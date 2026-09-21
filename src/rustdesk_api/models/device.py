from __future__ import annotations

import datetime
import json
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rustdesk_api.db.database import Base
from rustdesk_api.models.tag import device_tags

if TYPE_CHECKING:
    from rustdesk_api.models.group import Group
    from rustdesk_api.models.share import DeviceShare
    from rustdesk_api.models.strategy import Strategy
    from rustdesk_api.models.tag import Tag
    from rustdesk_api.models.user import User


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _load_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, int) and not isinstance(v, bool)]


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (Index("ix_devices_owner_last_seen", "owner_id", "last_seen"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    # The RustDesk client/peer id. Treated as an identifier, not a username.
    # Uniqueness is scoped globally for this single-server deployment model;
    # if multi-tenant server pools are introduced later this must be
    # revisited (see CLAUDE.md section 44).
    rustdesk_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    # Stable client-generated UUID, distinct from the (rebindable) rustdesk_id.
    uuid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    alias: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str | None] = mapped_column(String(150), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Raw strings as reported by the client's own POST /api/sysinfo payload
    # (e.g. "8 X 86 Core(s)", "15.9GB / 31.9GB") - not parsed/normalized,
    # matching the reference project's approach (CLAUDE.md section 73).
    cpu: Mapped[str | None] = mapped_column(String(255), nullable=True)
    memory: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Free text an administrator or owner attaches (also what `rustdesk --assign
    # --note` sets).
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Ids of the incoming connections the client last reported in its heartbeat
    # (JSON list of ints; NULL = none). The ids only mean something to that one
    # running client process. Written only when the list changes.
    live_connections: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Ids an authorised user asked to end; handed to the client in its next
    # heartbeat response and then cleared (JSON list of ints).
    pending_disconnect: Mapped[str | None] = mapped_column(Text, nullable=True)

    # A different `uuid` than the one on record arrived in an unauthenticated
    # sysinfo upload. It is parked here until the owner or an administrator
    # accepts it (see services.devices.register_or_update), so a stranger who
    # knows the id cannot take the device over.
    pending_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_uuid_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pending_uuid_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # "approved" (managed as usual), "pending" (an unknown device that arrived while
    # NEW_DEVICE_POLICY=approve: recorded, but given no policy and not reachable from
    # the WebUI) or "rejected" (an administrator turned it down: its uploads are
    # ignored). Devices that existed before this column stay "approved".
    approval: Mapped[str] = mapped_column(
        String(10), nullable=False, default="approved", server_default="approved", index=True
    )

    last_seen: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # "http" or "https": how the client's last heartbeat reached this server (as seen
    # through a trusted proxy). NULL until the first heartbeat after this was added.
    api_scheme: Mapped[str | None] = mapped_column(String(5), nullable=True)
    # When we last put the strategy into a heartbeat response; limits how often a
    # strategy the client already has is sent again (see services.strategies).
    strategy_sent_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # An administrator asked to be told when this device stops reporting (see
    # services.notifications). `offline_notified_at` remembers that it was, so one
    # outage is one notification; it is cleared when the device is back.
    watch_offline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    offline_notified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when the device has been silent for DEVICE_STALE_DAYS: it drops out of the
    # default device list and the counts, and comes back by itself when it reports.
    archived_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Client options pushed to this device; falls back to its group's strategy.
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    # No RustDesk device password/credential field: Phase 1's endpoints
    # (login, heartbeat, sysinfo, device listing) never need to read one
    # back, and CLAUDE.md section 18 treats such credentials as highly
    # sensitive - deferred until a concrete feature actually requires
    # persisting one, at which point it must be encrypted at rest.

    owner: Mapped[User | None] = relationship(  # noqa: F821
        "User", back_populates="devices", foreign_keys=[owner_id]
    )
    group: Mapped[Group | None] = relationship("Group", foreign_keys=[group_id])  # noqa: F821
    strategy: Mapped[Strategy | None] = relationship("Strategy", foreign_keys=[strategy_id])  # noqa: F821
    tags: Mapped[list[Tag]] = relationship(  # noqa: F821
        "Tag", secondary=device_tags, back_populates="devices"
    )
    shares: Mapped[list[DeviceShare]] = relationship(  # noqa: F821
        "DeviceShare",
        back_populates="device",
        cascade="all, delete-orphan",
        foreign_keys="DeviceShare.device_id",
    )

    @property
    def connection_ids(self) -> list[int]:
        return _load_ids(self.live_connections)

    @property
    def pending_disconnect_ids(self) -> list[int]:
        return _load_ids(self.pending_disconnect)

    @property
    def is_approved(self) -> bool:
        # A device not yet flushed has no value: the column default makes it "approved".
        return self.approval in (None, "approved")

    def is_online(self, timeout_seconds: int) -> bool:
        if self.last_seen is None:
            return False
        last_seen = self.last_seen
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=datetime.timezone.utc)
        return (_utcnow() - last_seen).total_seconds() <= timeout_seconds

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Device id={self.id} rustdesk_id={self.rustdesk_id!r}>"
