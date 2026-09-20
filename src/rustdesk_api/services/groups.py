from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from rustdesk_api.models.group import Group


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def create_group(db: Session, *, owner_id: int, name: str, description: str | None = None) -> Group:
    group = Group(owner_id=owner_id, name=name, description=description)
    db.add(group)
    db.flush()
    return group


def get_by_id(db: Session, group_id: int) -> Group | None:
    return db.get(Group, group_id)


def list_groups(db: Session, *, owner_id: int | None) -> list[Group]:
    """owner_id=None means "all groups" (admin use only - callers must
    enforce that)."""
    stmt = select(Group).order_by(Group.name)
    if owner_id is not None:
        stmt = stmt.where(Group.owner_id == owner_id)
    return list(db.execute(stmt).scalars())


def update_group(
    db: Session, group: Group, *, name: str | None = None, description: str | None = None
) -> Group:
    if name is not None:
        group.name = name
    if description is not None:
        group.description = description
    group.updated_at = _utcnow()
    db.flush()
    return group


def delete_group(db: Session, group: Group) -> None:
    db.delete(group)
