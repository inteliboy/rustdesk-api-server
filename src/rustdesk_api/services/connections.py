"""Live incoming connections of a device, and ending them.

The RustDesk client's heartbeat carries `conns`: the ids of the incoming
connections it currently has (omitted when there are none). A heartbeat
*response* with `disconnect: [ids]` makes it close exactly those
(`hbbs_http/sync.rs`, in 1.4.9 as well as `master`). The ids only mean
something to that one running client process: they restart from scratch when
it does.

Nothing here is a second source of truth about who is connected - it is what
the client last said, and a connection that ended since then is dropped from
the request as soon as the next heartbeat arrives.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from rustdesk_api.models.client_audit import ConnectionLog
from rustdesk_api.models.device import Device
from rustdesk_api.services import client_audit

# A client has a handful of incoming sessions at most; anything larger is junk.
MAX_CONNECTIONS = 64


def clean_connection_ids(raw: object) -> list[int]:
    """The ids from a heartbeat's `conns`, sorted and de-duplicated. Anything
    that is not a plain positive integer is ignored."""
    if not isinstance(raw, list):
        return []
    ids = {v for v in raw if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 2**31}
    return sorted(ids)[:MAX_CONNECTIONS]


def _dump(ids: list[int]) -> str | None:
    return json.dumps(ids) if ids else None


def record_heartbeat(db: Session, device: Device, reported: object) -> list[int]:
    """Stores the connection ids the client just reported (only when they
    changed, so a steady heartbeat writes nothing), forgets pending
    disconnects for connections that are gone, and returns the ids that should
    be closed now (the response's `disconnect`).
    """
    live = clean_connection_ids(reported)
    encoded = _dump(live)
    if device.live_connections != encoded:
        device.live_connections = encoded

    pending = device.pending_disconnect_ids
    if not pending:
        return []
    still_live = [conn_id for conn_id in pending if conn_id in live]
    # Handed over once: if the response is lost the administrator can press the
    # button again, which is safer than a stale id closing a different
    # connection that later reuses it.
    device.pending_disconnect = None
    return still_live


def request_disconnect(device: Device, requested: list[int] | None) -> list[int]:
    """Queue connections to be ended at the device's next heartbeat. `None`
    means every connection it currently reports. Returns the ids queued;
    raises `ValueError` if a requested id is not currently connected."""
    live = device.connection_ids
    if requested is None:
        chosen = list(live)
    else:
        unknown = [conn_id for conn_id in requested if conn_id not in live]
        if unknown:
            raise ValueError("Not a current connection of this device.")
        chosen = sorted(set(requested))
    if chosen:
        device.pending_disconnect = _dump(sorted(set(device.pending_disconnect_ids) | set(chosen)))
    return chosen


def describe(db: Session, device: Device) -> list[dict]:
    """The live connections with whatever the client's own connection log says
    about them (who is connecting), for the device page."""
    live = device.connection_ids
    if not live:
        return []
    rows = db.execute(
        select(ConnectionLog)
        .where(
            ConnectionLog.device_id == device.id,
            ConnectionLog.ended_at.is_(None),
            ConnectionLog.conn_id.in_([str(conn_id) for conn_id in live]),
        )
        .order_by(ConnectionLog.started_at.desc())
    ).scalars()
    by_conn: dict[int, ConnectionLog] = {}
    for row in rows:
        if row.conn_id is not None and row.conn_id.isdigit():
            by_conn.setdefault(int(row.conn_id), row)
    pending = set(device.pending_disconnect_ids)
    names = client_audit.restore_peer_names(db, {(r.peer_id, r.peer_name) for r in by_conn.values()})
    result = []
    for conn_id in live:
        row = by_conn.get(conn_id)
        result.append(
            {
                "id": conn_id,
                "peer_id": row.peer_id if row else None,
                "peer_name": names.get((row.peer_id or "", row.peer_name or ""), row.peer_name)
                if row
                else None,
                "from_ip": row.from_ip if row else None,
                "started_at": row.started_at if row else None,
                "disconnect_requested": conn_id in pending,
            }
        )
    return result
