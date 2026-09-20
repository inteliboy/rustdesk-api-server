from __future__ import annotations

import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from rustdesk_api.db.database import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class DeviceEvent(Base):
    """Something that happened to a device and is not in the audit log: it came
    back online, or an unknown install tried to take its id over. Kept short and
    pruned with the audit log."""

    __tablename__ = "device_events"
    __table_args__ = (Index("ix_device_events_device_created", "device_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
