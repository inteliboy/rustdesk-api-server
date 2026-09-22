from __future__ import annotations

import datetime
import json

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from rustdesk_api.db.database import Base

# The console areas a role's permission matrix covers, and the levels each area can
# be set to. Modelled on CortenDesk's role matrix: a role only ever narrows or widens
# what someone can *do* in these areas - it never changes which devices they can
# *see* (that stays governed by device ownership/sharing in security/permissions.py).
PERMISSION_AREAS = (
    "devices",
    "users",
    "groups",
    "address_books",
    "logs",
    "policies",
    "settings",
    "tokens",
)
PERMISSION_LEVELS = ("none", "view", "manage")


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Role(Base):
    """A named permission matrix (CortenDesk-style): for each of PERMISSION_AREAS, one
    of PERMISSION_LEVELS. Assigned directly to a user (User.role_id) or to a UserGroup
    that a user belongs to; see security/permissions.effective_permissions for how the
    two are combined. `requires_2fa` forces every user who ends up with this role
    (directly or through a group) to have two-factor authentication enabled."""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # JSON object of area -> level (same on-disk convention as Strategy.config_options).
    # An area missing from the object is "none".
    permissions_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    requires_2fa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    @property
    def permissions(self) -> dict[str, str]:
        try:
            value = json.loads(self.permissions_json or "{}")
        except ValueError:
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            str(area): str(level)
            for area, level in value.items()
            if area in PERMISSION_AREAS and level in PERMISSION_LEVELS
        }

    @permissions.setter
    def permissions(self, value: dict[str, str]) -> None:
        cleaned = {
            area: level
            for area, level in value.items()
            if area in PERMISSION_AREAS and level in PERMISSION_LEVELS and level != "none"
        }
        self.permissions_json = json.dumps(cleaned)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Role id={self.id} name={self.name!r}>"
