from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rustdesk_api.db.database import Base
from rustdesk_api.models.user_group import user_group_members

if TYPE_CHECKING:
    from rustdesk_api.models.device import Device
    from rustdesk_api.models.role import Role
    from rustdesk_api.models.session import AuthSession
    from rustdesk_api.models.user_group import UserGroup


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150), unique=True, index=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # A directly-assigned role (CortenDesk-style permission matrix; see
    # security/permissions.effective_permissions). Independent of is_admin, which
    # stays a full bypass - a role only ever grants a non-admin partial access.
    role_id: Mapped[int | None] = mapped_column(
        ForeignKey("roles.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Two-factor authentication (TOTP). The secret is stored encrypted with
    # DATA_ENCRYPTION_KEY, which is why enabling it requires that key.
    # `totp_last_step` is the newest 30 s time step already accepted, so a code
    # cannot be used twice.
    totp_secret_enc: Mapped[str | None] = mapped_column(String(255), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Wrong passwords in a row; at LOGIN_LOCKOUT_THRESHOLD the account is locked
    # until `locked_until`. Reset by a successful login or by an administrator.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    devices: Mapped[list[Device]] = relationship(  # noqa: F821
        "Device", back_populates="owner", foreign_keys="Device.owner_id"
    )
    sessions: Mapped[list[AuthSession]] = relationship(  # noqa: F821
        "AuthSession", back_populates="user", cascade="all, delete-orphan"
    )
    role: Mapped[Role | None] = relationship("Role", foreign_keys=[role_id])  # noqa: F821
    user_groups: Mapped[list[UserGroup]] = relationship(  # noqa: F821
        "UserGroup", secondary=user_group_members, back_populates="members"
    )

    @property
    def two_factor_enabled(self) -> bool:
        return bool(self.totp_enabled)

    @property
    def has_password(self) -> bool:
        """False for an account provisioned by OIDC or LDAP that has never had a
        local password set (services.oidc.UNUSABLE_PASSWORD): it can only sign
        in through that provider, or after an administrator issues a reset link."""
        return not self.password_hash.startswith("!")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<User id={self.id} username={self.username!r} is_admin={self.is_admin}>"
