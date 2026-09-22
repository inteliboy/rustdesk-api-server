from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Column, DateTime, ForeignKey, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rustdesk_api.db.database import Base

if TYPE_CHECKING:
    from rustdesk_api.models.role import Role
    from rustdesk_api.models.user import User


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# A grouping of *users* that a role can be assigned to, so a grant is made once for
# everyone in it - distinct from the existing Group model, which groups *devices*.
user_group_members = Table(
    "user_group_members",
    Base.metadata,
    Column("user_group_id", ForeignKey("user_groups.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


class UserGroup(Base):
    """A named group of users with one optional role. Everyone in the group gets
    that role's permissions in addition to whatever role they hold directly
    (see security/permissions.effective_permissions)."""

    __tablename__ = "user_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    role_id: Mapped[int | None] = mapped_column(
        ForeignKey("roles.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    role: Mapped[Role | None] = relationship("Role", foreign_keys=[role_id])  # noqa: F821
    members: Mapped[list[User]] = relationship(  # noqa: F821
        "User", secondary=user_group_members, back_populates="user_groups"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UserGroup id={self.id} name={self.name!r}>"
