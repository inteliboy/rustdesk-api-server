from __future__ import annotations

import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from rustdesk_api.db.database import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class LdapIdentity(Base):
    """An account in the configured LDAP/Active Directory directory, linked to a
    local user. Unlike OidcIdentity there is only ever one configured directory
    (LDAP_*), so the username alone identifies the remote account; it is stored
    lower-cased since most directories treat usernames case-insensitively."""

    __tablename__ = "ldap_identities"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    ldap_username: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    distinguished_name: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LdapIdentity user_id={self.user_id} ldap_username={self.ldap_username!r}>"
