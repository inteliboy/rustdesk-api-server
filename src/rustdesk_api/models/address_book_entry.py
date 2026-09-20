from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rustdesk_api.db.database import Base

if TYPE_CHECKING:
    from rustdesk_api.models.user import User


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _new_guid() -> str:
    return str(uuid.uuid4())


# Share rules, exactly as the RustDesk client defines them (`ShareRule` in
# flutter/lib/common/hbbs/hbbs.dart). The client only ever asks "can I
# write?" (rule 2 or 3) - it never treats "full control" differently - so the
# API gives 2 and 3 the same entry/tag permissions.
RULE_READ = 1
RULE_READ_WRITE = 2
RULE_FULL_CONTROL = 3
VALID_RULES = (RULE_READ, RULE_READ_WRITE, RULE_FULL_CONTROL)

# The client names the personal book itself ("My address book") and never
# expects it from the server; the server-side name only has to be unique per
# owner, and shared books may not take it.
PERSONAL_BOOK_NAME = "My address book"

# ARGB, as the client stores tag colors (`Color.value`): opaque slate.
DEFAULT_TAG_COLOR = 0xFF64748B


class AddressBook(Base):
    """One address book: a user's personal book, or a shared book.

    The personal book is what the legacy `/api/ab` endpoints read and write,
    so a client in legacy mode and one in the newer per-item mode see the same
    entries. Shared books exist only in the newer protocol
    (`/api/ab/shared/profiles`).
    """

    __tablename__ = "address_books"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_address_book_owner_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Opaque id the client puts in URLs (`/api/ab/peers?ab=<guid>`). Being
    # unguessable is not access control - every request re-checks the caller's
    # rule on the book.
    guid: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=_new_guid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_personal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    owner: Mapped[User] = relationship("User", foreign_keys=[owner_id])  # noqa: F821
    shares: Mapped[list[AddressBookShare]] = relationship(
        "AddressBookShare", back_populates="book", cascade="all, delete-orphan", passive_deletes=True
    )
    entries: Mapped[list[AddressBookEntry]] = relationship(
        "AddressBookEntry", back_populates="book", cascade="all, delete-orphan", passive_deletes=True
    )
    tags: Mapped[list[AddressBookTag]] = relationship(
        "AddressBookTag", back_populates="book", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AddressBook id={self.id} name={self.name!r} personal={self.is_personal}>"


class AddressBookShare(Base):
    """A user's access (`rule`) to somebody else's shared address book."""

    __tablename__ = "address_book_shares"
    __table_args__ = (UniqueConstraint("address_book_id", "user_id", name="uq_ab_share_book_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    address_book_id: Mapped[int] = mapped_column(
        ForeignKey("address_books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule: Mapped[int] = mapped_column(Integer, nullable=False, default=RULE_READ)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    book: Mapped[AddressBook] = relationship("AddressBook", back_populates="shares")
    user: Mapped[User] = relationship("User", foreign_keys=[user_id])  # noqa: F821


class AddressBookTag(Base):
    """A tag inside one address book. Per-book (not the global device `Tag`
    vocabulary), with the client's own ARGB integer color."""

    __tablename__ = "address_book_tags"
    __table_args__ = (UniqueConstraint("address_book_id", "name", name="uq_ab_tag_book_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    address_book_id: Mapped[int] = mapped_column(
        ForeignKey("address_books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[int] = mapped_column(BigInteger, nullable=False, default=DEFAULT_TAG_COLOR)

    book: Mapped[AddressBook] = relationship("AddressBook", back_populates="tags")

    @property
    def color_hex(self) -> str:
        """`#rrggbb` for the WebUI (alpha dropped)."""
        return f"#{self.color & 0xFFFFFF:06x}"


address_book_entry_tags = Table(
    "address_book_entry_tags",
    Base.metadata,
    Column("entry_id", ForeignKey("address_book_entries.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("address_book_tags.id", ondelete="CASCADE"), primary_key=True),
)


class AddressBookEntry(Base):
    """A single peer saved in an address book.

    Confirmed (2026-09-18) via live capture that the RustDesk client
    manages these independently of anything this server auto-registers via
    /api/sysinfo - a user can add an entry for a peer id this server has
    never seen a heartbeat/sysinfo call for. See
    docs/rustdesk-compatibility.md. Not the same thing as `Device`: a
    `Device` is something *this server* has observed reporting in; an
    `AddressBookEntry` is something *a user's client* has saved, which may
    or may not correspond to a known `Device`.
    """

    __tablename__ = "address_book_entries"
    __table_args__ = (
        UniqueConstraint("address_book_id", "rustdesk_id", name="uq_ab_entry_book_rustdesk_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    address_book_id: Mapped[int] = mapped_column(
        ForeignKey("address_books.id", ondelete="CASCADE"), nullable=False, index=True
    )

    rustdesk_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(150), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    alias: Mapped[str | None] = mapped_column(String(255), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Client-computed, opaque (personal books only - the client sends a
    # `password` instead for shared books, which is deliberately not stored;
    # see docs/rustdesk-compatibility.md). Treat as credential-adjacent
    # (CLAUDE.md section 18) - never logged, never returned anywhere except
    # back into this same address-book round trip.
    hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # A shared book's connection password, Fernet-encrypted with
    # DATA_ENCRYPTION_KEY (security/encryption.py). Unlike `hash` the client
    # sends and expects this in clear, so the server has to be able to give it
    # back. Only ever written when a key is configured; never logged and never
    # returned by the WebUI management API.
    password_enc: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    book: Mapped[AddressBook] = relationship("AddressBook", back_populates="entries")
    tags: Mapped[list[AddressBookTag]] = relationship(  # noqa: F821
        "AddressBookTag", secondary=address_book_entry_tags
    )

    @property
    def has_password(self) -> bool:
        """Whether a connection password has been synced for this entry -
        exposed instead of `hash` itself in the management API (CLAUDE.md
        section 18), matching the reference project's own has_rhash
        indicator."""
        return bool(self.hash or self.password_enc)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<AddressBookEntry id={self.id} book_id={self.address_book_id} rustdesk_id={self.rustdesk_id!r}>"
        )
