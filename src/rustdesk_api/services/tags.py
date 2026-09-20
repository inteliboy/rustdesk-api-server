from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from rustdesk_api.models.device import Device
from rustdesk_api.models.tag import Tag

DEFAULT_TAG_COLOR = "#64748b"


def create_tag(db: Session, *, name: str, color: str | None = None) -> Tag:
    tag = Tag(name=name, color=color or DEFAULT_TAG_COLOR)
    db.add(tag)
    db.flush()
    return tag


def get_by_id(db: Session, tag_id: int) -> Tag | None:
    return db.get(Tag, tag_id)


def get_by_name(db: Session, name: str) -> Tag | None:
    stmt = select(Tag).where(Tag.name == name)
    return db.execute(stmt).scalar_one_or_none()


def list_tags(db: Session) -> list[Tag]:
    return list(db.execute(select(Tag).order_by(Tag.name)).scalars())


def update_tag(db: Session, tag: Tag, *, name: str | None = None, color: str | None = None) -> Tag:
    if name is not None:
        tag.name = name
    if color is not None:
        tag.color = color
    db.flush()
    return tag


def delete_tag(db: Session, tag: Tag) -> None:
    db.delete(tag)


def set_device_tags(db: Session, device: Device, tags: list[Tag]) -> Device:
    device.tags = tags
    db.flush()
    return device
