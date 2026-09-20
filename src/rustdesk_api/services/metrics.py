"""Prometheus metrics (text exposition format, written by hand: it is a few
lines of text and not worth a dependency).

Gauges are computed from the database at scrape time, so they are right with
any number of worker processes. The two counters (heartbeats and system-info
uploads) live in memory, per process, and restart from zero - Prometheus'
`rate()` copes with that, but with several workers each one answers with its
own count, so use them for trends rather than exact totals.
"""

from __future__ import annotations

import datetime
import json
import threading

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rustdesk_api import __version__
from rustdesk_api.config import Settings
from rustdesk_api.models.audit import AuditLog
from rustdesk_api.models.device import Device
from rustdesk_api.models.session import AuthSession
from rustdesk_api.models.user import User


class Counters:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values = {"heartbeats": 0, "sysinfo_uploads": 0}

    def inc(self, name: str) -> None:
        with self._lock:
            self._values[name] = self._values.get(name, 0) + 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\n")


def render(db: Session, settings: Settings, counters: Counters) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    lines: list[str] = []

    def metric(name: str, kind: str, help_text: str, samples: list[tuple[dict[str, str], float]]) -> None:
        lines.append(f"# HELP rustdesk_api_{name} {help_text}")
        lines.append(f"# TYPE rustdesk_api_{name} {kind}")
        for labels, value in samples:
            label_text = ""
            if labels:
                label_text = "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"
            lines.append(f"rustdesk_api_{name}{label_text} {value}")

    def gauge(name: str, help_text: str, value: float) -> None:
        metric(name, "gauge", help_text, [({}, value)])

    def count(stmt) -> int:
        return db.execute(stmt).scalar_one()

    metric("info", "gauge", "Server version.", [({"version": __version__}, 1)])

    online_cutoff = now - datetime.timedelta(seconds=settings.device_online_timeout)
    total = count(select(func.count(Device.id)))
    online = count(select(func.count(Device.id)).where(Device.last_seen >= online_cutoff))
    gauge("devices", "Known devices.", total)
    gauge("devices_online", "Devices seen within DEVICE_ONLINE_TIMEOUT.", online)
    gauge("devices_offline", "Devices not seen within DEVICE_ONLINE_TIMEOUT.", total - online)
    gauge(
        "devices_uuid_change_pending",
        "Devices waiting for a decision on a uuid change.",
        count(select(func.count(Device.id)).where(Device.pending_uuid.is_not(None))),
    )

    live = 0
    stored = select(Device.live_connections).where(Device.live_connections.is_not(None))
    for raw in db.execute(stored).scalars():
        if raw is None:
            continue
        try:
            ids = json.loads(raw)
        except ValueError:
            continue  # unreadable value: not counted
        live += len(ids) if isinstance(ids, list) else 0
    gauge("connections_open", "Incoming connections reported at the clients' last heartbeat.", live)

    gauge("users", "User accounts.", count(select(func.count(User.id))))
    gauge(
        "users_active",
        "Active user accounts.",
        count(select(func.count(User.id)).where(User.is_active.is_(True))),
    )
    gauge(
        "users_locked",
        "Accounts locked by failed sign-ins.",
        count(select(func.count(User.id)).where(User.locked_until > now)),
    )

    session_rows = db.execute(
        select(AuthSession.kind, func.count(AuthSession.id))
        .where(AuthSession.revoked_at.is_(None), AuthSession.expires_at > now)
        .group_by(AuthSession.kind)
    ).all()
    metric(
        "sessions_active",
        "gauge",
        "Unexpired, unrevoked sessions and tokens by kind.",
        [({"kind": kind}, n) for kind, n in sorted(session_rows)],
    )

    since = now - datetime.timedelta(hours=24)
    login_rows = db.execute(
        select(AuditLog.result, func.count(AuditLog.id))
        .where(AuditLog.action == "login", AuditLog.created_at >= since)
        .group_by(AuditLog.result)
    ).all()
    metric(
        "logins_24h",
        "gauge",
        "Sign-in attempts in the last 24 hours by result (from the audit log).",
        [({"result": result}, n) for result, n in sorted(login_rows)],
    )

    seen = counters.snapshot()
    metric("heartbeats_total", "counter", "Heartbeats received by this process.", [({}, seen["heartbeats"])])
    metric(
        "sysinfo_uploads_total",
        "counter",
        "System-info uploads received by this process.",
        [({}, seen["sysinfo_uploads"])],
    )
    return "\n".join(lines) + "\n"
