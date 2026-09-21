"""Recording and querying RustDesk client connection / file-transfer / alarm
audit events (POST /api/audit/conn, /api/audit/file and /api/audit/alarm).

These events arrive unauthenticated (the real client sends no Authorization
header for them - verified against the client source, see
docs/rustdesk-compatibility.md), so every write path here is defensive:
events for unknown devices or with a mismatched device uuid are dropped,
strings are length-capped, and the file list is size-capped.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

from sqlalchemy import CursorResult, and_, delete, func, or_, select
from sqlalchemy.orm import Session

from rustdesk_api.models.client_audit import AlarmLog, ConnectionLog, FileTransferLog
from rustdesk_api.models.device import Device
from rustdesk_api.models.share import DeviceShare
from rustdesk_api.models.user import User
from rustdesk_api.services import devices as device_service

MAX_FILES_STORED = 200
MAX_STORED_FILE_NAME = 255
MAX_NOTE_LENGTH = 1000
# A session can be given a note while it is open and for this long after it
# ended (the client asks the user when the session closes, and the dialog can
# sit there for a while). Sessions that never reported a close count as open for
# a week at most.
NOTE_WINDOW = datetime.timedelta(hours=12)
OPEN_SESSION_LIFETIME = datetime.timedelta(days=7)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _clip(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def _as_int(value: Any) -> int | None:
    # bool is an int subclass but is never a valid enum value here.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def resolve_reporting_device(db: Session, rustdesk_id: str, uuid: str | None) -> Device | None:
    """The device an audit event may be attributed to, or None to drop it.

    Because the endpoint is unauthenticated, an event is only accepted for a
    device this server already knows, and - when a uuid is on record for
    it - only if the payload carries the same uuid. This is a spoofing
    speed-bump, not authentication: sysinfo (also unauthenticated) is what
    registered the device and uuid in the first place.
    """
    device = device_service.get_by_rustdesk_id(db, rustdesk_id)
    if device is None:
        return None
    if device.uuid and device.uuid != uuid:
        return None
    return device


def _find_open_connection(db: Session, rustdesk_id: str, conn_id: str | None) -> ConnectionLog | None:
    if conn_id is None:
        return None
    stmt = (
        select(ConnectionLog)
        .where(
            ConnectionLog.rustdesk_id == rustdesk_id,
            ConnectionLog.conn_id == conn_id,
            ConnectionLog.ended_at.is_(None),
        )
        .order_by(ConnectionLog.started_at.desc(), ConnectionLog.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def _nonce_seen(
    db: Session, model: type[ConnectionLog] | type[FileTransferLog] | type[AlarmLog], nonce: str | None
) -> bool:
    if not nonce:
        return False
    return db.execute(select(model.id).where(model.nonce == nonce)).first() is not None


def record_connection_event(
    db: Session,
    *,
    device: Device,
    conn_id: str | None,
    session_id: str | None,
    nonce: str | None,
    action: str | None,
    ip: str | None,
    peer: Any,
    conn_type: Any,
    primary_auth: Any,
    two_factor: Any,
) -> None:
    """Applies one /api/audit/conn payload. The client sends three shapes,
    told apart by which keys are present: `action: "new"` (session opened,
    carries `ip`), `action: "close"`, and an actor-less update carrying
    `peer`/`type` (session authorized)."""
    conn_id = _clip(conn_id, 32)

    if action == "new":
        nonce = _clip(nonce, 64)
        if _nonce_seen(db, ConnectionLog, nonce):
            return
        db.add(
            ConnectionLog(
                device_id=device.id,
                rustdesk_id=device.rustdesk_id,
                conn_id=conn_id,
                session_id=_clip(session_id, 64),
                from_ip=_clip(ip, 64),
                started_at=_utcnow(),
                nonce=nonce,
            )
        )
        return

    if action == "close":
        row = _find_open_connection(db, device.rustdesk_id, conn_id)
        if row is not None:
            row.ended_at = _utcnow()
        return

    # Authorization update. If the "new" event was lost, still keep the data.
    row = _find_open_connection(db, device.rustdesk_id, conn_id)
    if row is None:
        row = ConnectionLog(
            device_id=device.id,
            rustdesk_id=device.rustdesk_id,
            conn_id=conn_id,
            started_at=_utcnow(),
        )
        db.add(row)
    if session_id is not None:
        row.session_id = _clip(session_id, 64)
    if isinstance(peer, (list, tuple)) and len(peer) >= 2:
        row.peer_id = _clip(peer[0], 64)
        row.peer_name = _clip(peer[1], 255)
    row.conn_type = _as_int(conn_type)
    row.primary_auth = _as_int(primary_auth)
    row.two_factor = _as_int(two_factor)


def _clean_note(note: Any) -> str | None:
    """The note as stored, or None when there is nothing to store. Control
    characters are dropped; a note is shown as text and never interpreted."""
    if not isinstance(note, str):
        return None
    text = "".join(ch for ch in note if ch.isprintable() or ch in "\n\t").strip()
    return text[:MAX_NOTE_LENGTH] or None


def _open_or_recent(now: datetime.datetime):
    """SQL condition: the session is still open, or ended within NOTE_WINDOW."""
    return or_(
        and_(ConnectionLog.ended_at.is_(None), ConnectionLog.started_at > now - OPEN_SESSION_LIFETIME),
        ConnectionLog.ended_at > now - NOTE_WINDOW,
    )


def _session_row(
    db: Session, rustdesk_id: str, session_id: str, conn_type: int | None = None
) -> ConnectionLog | None:
    """The newest connection of `rustdesk_id` made with `session_id` that can
    still take a note. The client keeps one `session_id` across reconnects, and
    a remote-control and a file-transfer connection can share it, so the
    connection type tells those apart when the client says which one it means."""
    stmt = (
        select(ConnectionLog)
        .where(
            ConnectionLog.rustdesk_id == rustdesk_id,
            ConnectionLog.session_id == session_id,
            _open_or_recent(_utcnow()),
        )
        .order_by(ConnectionLog.started_at.desc(), ConnectionLog.id.desc())
    )
    for row in db.execute(stmt).scalars():
        if conn_type is None or row.conn_type is None or row.conn_type == conn_type:
            return row
    return None


def guid_for_session(db: Session, rustdesk_id: str, session_id: str, conn_type: int | None) -> str | None:
    """GET /api/audit/conn/active: the GUID the controlling client then sends
    back with its note. Made the first time it is asked for; None while the
    controlled device's own report of the session has not arrived (the client
    asks again a few times)."""
    row = _session_row(db, _clip(rustdesk_id, 64) or "", _clip(session_id, 64) or "", conn_type)
    if row is None:
        return None
    if row.guid is None:
        row.guid = str(uuid.uuid4())
    return row.guid


def set_note_by_guid(db: Session, guid: str, note: Any) -> bool:
    """PUT /api/audit. False when the GUID is unknown or its session is too old."""
    text = _clean_note(note)
    if text is None:
        return False
    row = db.execute(
        select(ConnectionLog).where(ConnectionLog.guid == guid[:36], _open_or_recent(_utcnow()))
    ).scalar_one_or_none()
    if row is None:
        return False
    row.note = text
    return True


def set_note_by_session(db: Session, rustdesk_id: str, session_id: str, note: Any) -> bool:
    """POST /api/audit/conn with {id, session_id, note}: the older (Sciter)
    client, which sends no token and no GUID. `session_id` is a random 64-bit
    number the two clients share, so together with the id it is what identifies
    the session; a wrong pair changes nothing."""
    text = _clean_note(note)
    if text is None:
        return False
    row = _session_row(db, _clip(rustdesk_id, 64) or "", _clip(session_id, 64) or "")
    if row is None:
        return False
    row.note = text
    return True


def _parse_info(info: Any) -> dict[str, Any]:
    """`info` is sent as a JSON-encoded *string* (the same protocol quirk as
    /api/ab's `data`), but tolerate a plain object too."""
    if isinstance(info, dict):
        return info
    if isinstance(info, str):
        try:
            parsed = json.loads(info)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _capped_files(raw: Any) -> str | None:
    if not isinstance(raw, list):
        return None
    capped: list[list[Any]] = []
    for item in raw[:MAX_FILES_STORED]:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            name = str(item[0])[:MAX_STORED_FILE_NAME]
            size = item[1] if isinstance(item[1], int) and not isinstance(item[1], bool) else None
            capped.append([name, size])
    return json.dumps(capped)


def record_file_event(
    db: Session,
    *,
    device: Device,
    conn_id: str | None,
    peer_id: str | None,
    nonce: str | None,
    audit_type: Any,
    path: str | None,
    is_file: Any,
    info: Any,
) -> None:
    nonce = _clip(nonce, 64)
    if _nonce_seen(db, FileTransferLog, nonce):
        return
    parsed = _parse_info(info)
    num = _as_int(parsed.get("num"))
    db.add(
        FileTransferLog(
            device_id=device.id,
            rustdesk_id=device.rustdesk_id,
            conn_id=_clip(conn_id, 32),
            peer_id=_clip(peer_id, 64),
            peer_name=_clip(parsed.get("name"), 255),
            from_ip=_clip(parsed.get("ip"), 64),
            audit_type=_as_int(audit_type),
            path=_clip(path, 1024),
            is_file=bool(is_file) if isinstance(is_file, bool) else False,
            num=num,
            files=_capped_files(parsed.get("files")),
            logged_at=_utcnow(),
            nonce=nonce,
        )
    )


def record_alarm_event(
    db: Session,
    *,
    device: Device,
    alarm_type: Any,
    conn_id: str | None,
    nonce: str | None,
    info: Any,
) -> None:
    """Applies one /api/audit/alarm payload.

    `info` is a JSON-encoded string whose keys depend on the alarm type
    (verified against connection.rs): `ip` always, `id` + `name` (the peer that
    was refused) for most types, and `conn_type` + `message` for a session
    scope violation. Only those known keys are read, each length-capped; the
    raw `info` is never stored, so a client cannot park arbitrary data here.
    """
    nonce = _clip(nonce, 64)
    if _nonce_seen(db, AlarmLog, nonce):
        return
    parsed = _parse_info(info)
    db.add(
        AlarmLog(
            device_id=device.id,
            rustdesk_id=device.rustdesk_id,
            alarm_type=_as_int(alarm_type),
            conn_id=_clip(conn_id, 32),
            from_ip=_clip(parsed.get("ip"), 64),
            peer_id=_clip(parsed.get("id"), 64),
            peer_name=_clip(parsed.get("name"), 255),
            conn_type=_clip(parsed.get("conn_type"), 32),
            message=_clip(parsed.get("message"), 255),
            logged_at=_utcnow(),
            nonce=nonce,
        )
    )


def _visible_device_ids(user: User):
    """Devices whose logs this user may read: owned, or actively shared."""
    shared = select(DeviceShare.device_id).where(
        DeviceShare.shared_with_user_id == user.id,
        or_(DeviceShare.expires_at.is_(None), DeviceShare.expires_at > _utcnow()),
    )
    return select(Device.id).where(or_(Device.owner_id == user.id, Device.id.in_(shared)))


def list_connection_logs(
    db: Session, *, user: User, device_id: int | None, page: int, page_size: int
) -> tuple[list[ConnectionLog], int]:
    stmt = select(ConnectionLog)
    count_stmt = select(func.count(ConnectionLog.id))
    if not user.is_admin:
        cond = ConnectionLog.device_id.in_(_visible_device_ids(user))
        stmt, count_stmt = stmt.where(cond), count_stmt.where(cond)
    if device_id is not None:
        stmt = stmt.where(ConnectionLog.device_id == device_id)
        count_stmt = count_stmt.where(ConnectionLog.device_id == device_id)
    total = db.execute(count_stmt).scalar_one()
    stmt = (
        stmt.order_by(ConnectionLog.started_at.desc(), ConnectionLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(db.execute(stmt).scalars()), total


def list_file_logs(
    db: Session, *, user: User, device_id: int | None, page: int, page_size: int
) -> tuple[list[FileTransferLog], int]:
    stmt = select(FileTransferLog)
    count_stmt = select(func.count(FileTransferLog.id))
    if not user.is_admin:
        cond = FileTransferLog.device_id.in_(_visible_device_ids(user))
        stmt, count_stmt = stmt.where(cond), count_stmt.where(cond)
    if device_id is not None:
        stmt = stmt.where(FileTransferLog.device_id == device_id)
        count_stmt = count_stmt.where(FileTransferLog.device_id == device_id)
    total = db.execute(count_stmt).scalar_one()
    stmt = (
        stmt.order_by(FileTransferLog.logged_at.desc(), FileTransferLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(db.execute(stmt).scalars()), total


def list_alarm_logs(
    db: Session, *, user: User, device_id: int | None, page: int, page_size: int
) -> tuple[list[AlarmLog], int]:
    stmt = select(AlarmLog)
    count_stmt = select(func.count(AlarmLog.id))
    if not user.is_admin:
        cond = AlarmLog.device_id.in_(_visible_device_ids(user))
        stmt, count_stmt = stmt.where(cond), count_stmt.where(cond)
    if device_id is not None:
        stmt = stmt.where(AlarmLog.device_id == device_id)
        count_stmt = count_stmt.where(AlarmLog.device_id == device_id)
    total = db.execute(count_stmt).scalar_one()
    stmt = (
        stmt.order_by(AlarmLog.logged_at.desc(), AlarmLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(db.execute(stmt).scalars()), total


def delete_connection_log(db: Session, log_id: int) -> ConnectionLog | None:
    """Removes one row; returns it (for the caller's audit entry) or None."""
    row = db.get(ConnectionLog, log_id)
    if row is not None:
        db.delete(row)
    return row


def delete_file_log(db: Session, log_id: int) -> FileTransferLog | None:
    row = db.get(FileTransferLog, log_id)
    if row is not None:
        db.delete(row)
    return row


def delete_alarm_log(db: Session, log_id: int) -> AlarmLog | None:
    row = db.get(AlarmLog, log_id)
    if row is not None:
        db.delete(row)
    return row


def clear_connection_logs(db: Session, *, device_id: int | None) -> int:
    """Deletes every connection log, or only one device's. Returns the count."""
    stmt = delete(ConnectionLog)
    if device_id is not None:
        stmt = stmt.where(ConnectionLog.device_id == device_id)
    result: CursorResult = db.execute(stmt)  # type: ignore[assignment]
    return result.rowcount


def clear_file_logs(db: Session, *, device_id: int | None) -> int:
    stmt = delete(FileTransferLog)
    if device_id is not None:
        stmt = stmt.where(FileTransferLog.device_id == device_id)
    result: CursorResult = db.execute(stmt)  # type: ignore[assignment]
    return result.rowcount


def clear_alarm_logs(db: Session, *, device_id: int | None) -> int:
    stmt = delete(AlarmLog)
    if device_id is not None:
        stmt = stmt.where(AlarmLog.device_id == device_id)
    result: CursorResult = db.execute(stmt)  # type: ignore[assignment]
    return result.rowcount


def device_labels(db: Session, device_ids: set[int]) -> dict[int, str]:
    """id -> display name (alias, else hostname) in one query, avoiding N+1."""
    if not device_ids:
        return {}
    rows = db.execute(select(Device.id, Device.alias, Device.hostname).where(Device.id.in_(device_ids))).all()
    return {row.id: (row.alias or row.hostname or "") for row in rows}


def restore_peer_names(db: Session, pairs: set[tuple[str | None, str | None]]) -> dict[tuple[str, str], str]:
    """(peer id, reported name) -> the name in its original letter case.

    The controlling client capitalizes the first letter of every word of its own
    name before sending it (client.rs, "display_name"), so "inteliboy" arrives as
    "Inteliboy" and the original is lost. Where the same name is known here, the
    peer device's own OS user name or one of this server's user names, and it
    differs from the reported one only in case, that spelling is used instead.
    Nothing new is revealed: the match is case-insensitive, so the reader already
    has the same letters. Names that match nothing are left as reported.
    """
    wanted = {(pid, name) for pid, name in pairs if name}
    if not wanted:
        return {}
    lowered = {name.casefold() for _, name in wanted}
    peer_ids = {pid for pid, _ in wanted if pid}

    device_names: dict[str, str] = {}
    if peer_ids:
        for rustdesk_id, username in db.execute(
            select(Device.rustdesk_id, Device.username).where(Device.rustdesk_id.in_(peer_ids))
        ):
            if username:
                device_names[rustdesk_id] = username
    user_names = {
        username.casefold(): username
        for (username,) in db.execute(select(User.username).where(func.lower(User.username).in_(lowered)))
    }

    restored: dict[tuple[str, str], str] = {}
    for pid, name in wanted:
        key = name.casefold()
        device_name = device_names.get(pid) if pid else None
        if device_name and device_name.casefold() == key:
            restored[(pid or "", name)] = device_name
        elif key in user_names:
            restored[(pid or "", name)] = user_names[key]
    return restored
