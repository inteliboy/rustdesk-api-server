from __future__ import annotations

import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from rustdesk_api.db.database import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class OidcIdentity(Base):
    """An account at the identity provider, linked to a local user. The pair
    (issuer, subject) is what identifies it; the e-mail address is only kept to
    show which account is linked and is never used to find one."""

    __tablename__ = "oidc_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject"),
        # One account per provider for a user keeps "unlink" and "link" unambiguous.
        UniqueConstraint("user_id", "issuer"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OidcRequest(Base):
    """One sign-in in progress, from the moment a browser is sent to the provider
    until its result is collected. Short-lived (see services/oidc.py) and purged.

    Nothing here can be used on its own: `state` and the poll handle are stored
    hashed, and the PKCE verifier and nonce are not stored at all (they are derived
    from `salt` and the server's secret key)."""

    __tablename__ = "oidc_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # The RustDesk client's poll handle (`code` in /api/oidc/auth-query); client requests only.
    handle_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    salt: Mapped[str] = mapped_column(String(64), nullable=False)
    # "client" (RustDesk client), "webui" (sign in) or "link" (attach to a signed-in user).
    purpose: Mapped[str] = mapped_column(String(10), nullable=False)
    # Hash of the cookie set in the browser that started a "webui"/"link" request.
    binding_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The id and uuid the RustDesk client started the request with; the poll must repeat them.
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_uuid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # "link": the user who is linking. "client"/"webui": the user the provider signed in.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # pending -> exchanging (the callback is talking to the provider) -> done | failed
    status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
