"""Export (/api/v1/export/...) and import (/api/v1/import/devices).

Exports are CSV or JSON. A device export holds what the caller may see; the
user and audit exports are administrator-only. Nothing secret is ever in them:
no password hashes, tokens, device uuids or two-factor data. Text cells that a
spreadsheet would run as a formula (they start with = + - @) are prefixed with a
quote, since device names and aliases are chosen by other people.

Import creates or updates devices' *management* data (alias, note, owner,
group, tags) from a file: for pre-filling a fresh server or moving over from
another one. It is administrator-only, checks every row before changing
anything, and applies nothing if any row is bad.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_current_admin, get_current_user, get_settings_dep, verify_csrf
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.audit import AuditLog
from rustdesk_api.models.device import Device
from rustdesk_api.models.group import Group
from rustdesk_api.models.tag import Tag
from rustdesk_api.models.user import User
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import tags as tag_service

router = APIRouter(prefix="/api/v1", tags=["import-export"])

MAX_EXPORT_ROWS = 100_000
MAX_IMPORT_ROWS = 5_000
MAX_IMPORT_BYTES = 2 * 1024 * 1024
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value: object) -> object:
    if isinstance(value, str) and value.startswith(_FORMULA_STARTS):
        return "'" + value
    return value


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _respond(rows: list[dict], columns: list[str], fmt: str, name: str) -> Response:
    if fmt == "json":
        body = json.dumps(rows, indent=2, default=str)
        media_type, filename = "application/json", f"{name}.json"
    else:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _safe_cell(v) for k, v in row.items()})
        body = buffer.getvalue()
        media_type, filename = "text/csv; charset=utf-8", f"{name}.csv"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _record_export(db: Session, user: User, what: str, count: int) -> None:
    audit_service.record(db, action="data_exported", actor_id=user.id, detail={"what": what, "rows": count})
    db.commit()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

DEVICE_COLUMNS = [
    "rustdesk_id", "alias", "hostname", "name", "platform", "os_version", "client_version", "ip_address",
    "owner", "group", "tags", "note", "online", "last_seen", "created_at",
]  # fmt: skip


@router.get("/export/devices")
def export_devices(
    format: Literal["csv", "json"] = Query(default="csv"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    rows: list[dict] = []
    page = 1
    while len(rows) < MAX_EXPORT_ROWS:
        items, _total = device_service.list_devices(
            db,
            owner_id=None if user.is_admin else user.id,
            shared_with_user_id=None if user.is_admin else user.id,
            sort="id",
            page=page,
            page_size=200,
        )
        for d in items:
            rows.append(
                {
                    "rustdesk_id": d.rustdesk_id,
                    "alias": d.alias,
                    "hostname": d.hostname,
                    "name": d.name,
                    "platform": d.platform,
                    "os_version": d.os_version,
                    "client_version": d.client_version,
                    "ip_address": d.ip_address,
                    "owner": d.owner.username if d.owner else None,
                    "group": d.group.name if d.group else None,
                    "tags": ";".join(t.name for t in d.tags),
                    "note": d.note,
                    "online": d.is_online(settings.device_online_timeout),
                    "last_seen": _iso(d.last_seen),
                    "created_at": _iso(d.created_at),
                }
            )
        if len(items) < 200:
            break
        page += 1
    _record_export(db, user, "devices", len(rows))
    return _respond(rows, DEVICE_COLUMNS, format, "devices")


USER_COLUMNS = ["username", "email", "is_admin", "is_active", "two_factor", "created_at", "last_login_at"]


@router.get("/export/users")
def export_users(
    format: Literal["csv", "json"] = Query(default="csv"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> Response:
    users = db.execute(select(User).order_by(User.username).limit(MAX_EXPORT_ROWS)).scalars()
    rows = [
        {
            "username": u.username,
            "email": u.email,
            "is_admin": u.is_admin,
            "is_active": u.is_active,
            "two_factor": u.totp_enabled,
            "created_at": _iso(u.created_at),
            "last_login_at": _iso(u.last_login_at),
        }
        for u in users
    ]
    _record_export(db, admin, "users", len(rows))
    return _respond(rows, USER_COLUMNS, format, "users")


AUDIT_COLUMNS = ["at", "actor", "action", "target_type", "target_id", "result", "ip_address", "detail"]


@router.get("/export/audit")
def export_audit(
    format: Literal["csv", "json"] = Query(default="csv"),
    days: int = Query(default=30, ge=1, le=3650),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> Response:
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    stmt = (
        select(AuditLog, User.username)
        .join(User, User.id == AuditLog.actor_id, isouter=True)
        .where(AuditLog.created_at >= since)
        .order_by(AuditLog.created_at.desc())
        .limit(MAX_EXPORT_ROWS)
    )
    rows = [
        {
            "at": _iso(a.created_at),
            "actor": actor,
            "action": a.action,
            "target_type": a.target_type,
            "target_id": a.target_id,
            "result": a.result,
            "ip_address": a.ip_address,
            "detail": a.detail,
        }
        for a, actor in db.execute(stmt).all()
    ]
    _record_export(db, admin, "audit", len(rows))
    return _respond(rows, AUDIT_COLUMNS, format, "audit")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

_ALIASES = {"id": "rustdesk_id", "rustdeskid": "rustdesk_id", "device_id": "rustdesk_id"}


def _norm_key(key: object) -> str:
    text = str(key).strip().lower().replace(" ", "_")
    return _ALIASES.get(text, text)


def _parse_rows(raw: bytes, content_type: str) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ApiError("INVALID_IMPORT", "The file must be UTF-8 text.", 422) from exc
    rows: list[dict]
    if "json" in content_type:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ApiError("INVALID_IMPORT", "The body is not valid JSON.", 422) from exc
        if isinstance(data, dict):
            data = data.get("rows")
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise ApiError("INVALID_IMPORT", 'Expected a list of device objects (or {"rows": [...]}).', 422)
        rows = data
    else:
        reader = csv.DictReader(io.StringIO(text))
        rows = [dict(r) for r in reader]
    if len(rows) > MAX_IMPORT_ROWS:
        raise ApiError("INVALID_IMPORT", f"At most {MAX_IMPORT_ROWS} rows per import.", 422)
    return [{_norm_key(k): v for k, v in row.items() if k is not None} for row in rows]


def _text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _tag_names(value: object) -> list[str]:
    if value is None or value == "":
        return []
    parts = value if isinstance(value, list) else str(value).replace(",", ";").split(";")
    names = [str(p).strip() for p in parts if str(p).strip()]
    return list(dict.fromkeys(n[:50] for n in names))


@router.post("/import/devices", dependencies=[Depends(verify_csrf)])
async def import_devices(
    request: Request,
    dry_run: bool = Query(default=False),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> dict:
    """Send `text/csv` (a header row with any of: rustdesk_id, alias, name, note,
    owner, group, tags) or `application/json` (a list of objects). `rustdesk_id`
    is required; an empty cell leaves that field as it is. Tags are separated
    by `;` and created if missing. Owner is a username and group a group name
    that must already exist."""
    raw = await request.body()
    if len(raw) > MAX_IMPORT_BYTES:
        raise ApiError("INVALID_IMPORT", "The file is too large.", 413)
    rows = _parse_rows(raw, request.headers.get("content-type", ""))
    if not rows:
        raise ApiError("INVALID_IMPORT", "The file has no rows.", 422)

    users = {u.username.lower(): u for u in db.execute(select(User)).scalars()}
    groups_by_name: dict[str, list[Group]] = {}
    for g in db.execute(select(Group)).scalars():
        groups_by_name.setdefault(g.name.lower(), []).append(g)
    known_tags = {t.name.lower(): t for t in db.execute(select(Tag)).scalars()}

    errors: list[dict] = []
    plan: list[dict] = []
    seen: set[str] = set()
    for number, row in enumerate(rows, start=1):
        problems: list[str] = []
        rustdesk_id = _text(row.get("rustdesk_id"), 65)
        if not rustdesk_id or len(rustdesk_id) > 64:
            problems.append("rustdesk_id is missing or longer than 64 characters")
        elif rustdesk_id in seen:
            problems.append(f"rustdesk_id {rustdesk_id} appears more than once")
        else:
            seen.add(rustdesk_id)

        owner = group = None
        owner_name = _text(row.get("owner"), 150)
        if owner_name:
            owner = users.get(owner_name.lower())
            if owner is None:
                problems.append(f"no user named {owner_name!r}")
        group_name = _text(row.get("group"), 100)
        if group_name:
            candidates = groups_by_name.get(group_name.lower(), [])
            if owner is not None and len(candidates) > 1:
                candidates = [g for g in candidates if g.owner_id == owner.id] or candidates
            if not candidates:
                problems.append(f"no group named {group_name!r}")
            elif len(candidates) > 1:
                problems.append(f"more than one group is named {group_name!r}")
            else:
                group = candidates[0]

        if problems:
            errors.append({"row": number, "message": "; ".join(problems)})
            continue
        plan.append(
            {
                "rustdesk_id": rustdesk_id,
                "alias": _text(row.get("alias"), 255),
                "name": _text(row.get("name"), 255),
                "note": _text(row.get("note"), 500),
                "owner": owner,
                "group": group,
                "tags": _tag_names(row.get("tags")),
            }
        )

    result = {
        "dry_run": dry_run,
        "applied": False,
        "created": 0,
        "updated": 0,
        "tags_created": 0,
        "errors": errors,
    }
    existing = device_service.get_by_rustdesk_ids(db, [p["rustdesk_id"] for p in plan])
    new_tag_names = {name.lower() for p in plan for name in p["tags"] if name.lower() not in known_tags}
    result["created"] = sum(1 for p in plan if p["rustdesk_id"] not in existing)
    result["updated"] = len(plan) - result["created"]
    result["tags_created"] = len(new_tag_names)
    if errors or dry_run:
        return result

    for name in sorted({n for p in plan for n in p["tags"] if n.lower() in new_tag_names}, key=str.lower):
        if name.lower() not in known_tags:
            known_tags[name.lower()] = tag_service.create_tag(db, name=name)
    for p in plan:
        device = existing.get(p["rustdesk_id"])
        if device is None:
            device = Device(rustdesk_id=p["rustdesk_id"])
            db.add(device)
            db.flush()
            device_service.record_event(db, device, "imported")
        if p["alias"] is not None:
            device.alias = p["alias"]
        if p["name"] is not None:
            device.name = p["name"]
        if p["note"] is not None:
            device.note = p["note"]
        if p["owner"] is not None:
            device.owner_id = p["owner"].id
        if p["group"] is not None:
            device.group_id = p["group"].id
        if p["tags"]:
            merged = {t.id: t for t in device.tags}
            for name in p["tags"]:
                tag = known_tags[name.lower()]
                merged[tag.id] = tag
            device.tags = list(merged.values())
        device.updated_at = datetime.datetime.now(datetime.timezone.utc)
    audit_service.record(
        db,
        action="devices_imported",
        actor_id=admin.id,
        detail={
            "created": result["created"],
            "updated": result["updated"],
            "tags_created": result["tags_created"],
        },
    )
    db.commit()
    result["applied"] = True
    return result
