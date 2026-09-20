from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rustdesk_api.db.database import Base

if TYPE_CHECKING:
    from rustdesk_api.models.user import User


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class AuthSession(Base):
    """A revocable, expirable authentication token.

    Only a hash of the token is stored; the raw token is returned to the
    client exactly once, at creation time, and is never persisted or logged.
    """

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    csrf_token: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # "webui" for browser sessions (cookie-based), "client" for RustDesk
    # desktop client logins (bearer-token based). Both share the same
    # underlying token/expiry mechanism.
    # "apikey" tokens are personal API keys for scripts: bearer-only, never a
    # cookie, and refused by the endpoints that manage the account itself.
    # "enroll" tokens are for `rustdesk --assign --token ...` only: every other
    # endpoint ignores them (see services.tokens.get_valid_session).
    kind: Mapped[str] = mapped_column(String(20), default="webui", nullable=False)
    # What an enrollment token is for, chosen by whoever created it.
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # "full" or "read". A "read" session (only API keys are ever created so) may
    # make GET/HEAD requests and nothing else - enforced in api.deps.
    scope: Mapped[str] = mapped_column(String(10), default="full", nullable=False)

    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="sessions")  # noqa: F821

    def is_valid(self) -> bool:
        if self.revoked_at is not None:
            return False
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        return _utcnow() < expires_at
