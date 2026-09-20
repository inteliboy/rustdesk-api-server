from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rustdesk_api.db.database import Base

if TYPE_CHECKING:
    from rustdesk_api.models.device import Device
    from rustdesk_api.models.user import User


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# Explicit permission levels (CLAUDE.md section 7 - Device Shares -
# "Permissions should be explicit rather than implicit"):
#   view    - can see the device in their list/detail view, read-only.
#   control - can also edit the device's alias/name. Never implies delete
#             or reassigning ownership - those stay owner/admin-only
#             regardless of share permission (CLAUDE.md section 12).
SHARE_PERMISSIONS = ("view", "control")


class DeviceShare(Base):
    __tablename__ = "device_shares"
    __table_args__ = (UniqueConstraint("device_id", "shared_with_user_id", name="uq_device_share_target"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shared_with_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    permission: Mapped[str] = mapped_column(String(20), nullable=False, default="view")

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    device: Mapped[Device] = relationship(  # noqa: F821
        "Device", back_populates="shares", foreign_keys=[device_id]
    )
    owner: Mapped[User] = relationship("User", foreign_keys=[owner_id])  # noqa: F821
    shared_with: Mapped[User] = relationship("User", foreign_keys=[shared_with_user_id])  # noqa: F821

    def is_active(self) -> bool:
        if self.expires_at is None:
            return True
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        return _utcnow() < expires_at

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DeviceShare device_id={self.device_id} shared_with_user_id={self.shared_with_user_id}>"
