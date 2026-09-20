from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Column, ForeignKey, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rustdesk_api.db.database import Base

if TYPE_CHECKING:
    from rustdesk_api.models.device import Device

# Tags are a global, shared vocabulary (CLAUDE.md section 7 - Tags - lists
# no owner_id, unlike Group), used through a many-to-many relationship with
# devices. Any authenticated user may create/use a tag; whether a given
# user may attach one to a specific device is still gated by that device's
# own ownership/share permissions.
device_tags = Table(
    "device_tags",
    Base.metadata,
    Column("device_id", ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="#64748b")

    devices: Mapped[list[Device]] = relationship(  # noqa: F821
        "Device", secondary=device_tags, back_populates="tags"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tag id={self.id} name={self.name!r}>"
