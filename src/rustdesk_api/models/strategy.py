from __future__ import annotations

import datetime
import json

from sqlalchemy import BigInteger, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from rustdesk_api.db.database import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Strategy(Base):
    """A named set of RustDesk client options the server pushes to devices in
    the response to their heartbeat (`strategy.config_options`). Assigned to a
    device or to a group; see services/strategies.py for what may be pushed."""

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # JSON object of option key -> value, both strings, exactly as the client
    # stores them ("Y"/"N" for switches).
    config_options: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Opaque version the client remembers (`strategy_timestamp`) and sends back
    # in every heartbeat; the strategy is re-sent only when it differs. Unix
    # microseconds of the last change, so it always moves forward.
    modified_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    @property
    def options(self) -> dict[str, str]:
        try:
            value = json.loads(self.config_options or "{}")
        except ValueError:
            return {}
        return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Strategy id={self.id} name={self.name!r}>"
