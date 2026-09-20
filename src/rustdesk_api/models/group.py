from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rustdesk_api.db.database import Base

if TYPE_CHECKING:
    from rustdesk_api.models.strategy import Strategy
    from rustdesk_api.models.user import User


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Group(Base):
    """A logical grouping of devices, owned by a single user
    (CLAUDE.md section 7 - Device Group). Group names are unique per owner,
    not globally, so two users can each have a group called "Servers"."""

    __tablename__ = "groups"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_groups_owner_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Client options pushed to every device in this group that has no strategy
    # of its own. Only an administrator can set it.
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    owner: Mapped[User] = relationship("User", foreign_keys=[owner_id])  # noqa: F821
    strategy: Mapped[Strategy | None] = relationship("Strategy", foreign_keys=[strategy_id])  # noqa: F821

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Group id={self.id} name={self.name!r} owner_id={self.owner_id}>"
