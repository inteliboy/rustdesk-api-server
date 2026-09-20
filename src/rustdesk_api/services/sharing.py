from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from rustdesk_api.models.device import Device
from rustdesk_api.models.share import SHARE_PERMISSIONS, DeviceShare


class InvalidPermission(Exception):
    pass


class AlreadyShared(Exception):
    pass


def create_share(
    db: Session,
    *,
    device: Device,
    owner_id: int,
    shared_with_user_id: int,
    permission: str,
    expires_at: datetime.datetime | None = None,
) -> DeviceShare:
    if permission not in SHARE_PERMISSIONS:
        raise InvalidPermission(f"permission must be one of {SHARE_PERMISSIONS!r}")
    if shared_with_user_id == owner_id:
        raise AlreadyShared("Cannot share a device with its own owner.")

    existing = get_share_for_user(db, device_id=device.id, user_id=shared_with_user_id)
    if existing is not None:
        raise AlreadyShared("This device is already shared with that user.")

    share = DeviceShare(
        device_id=device.id,
        owner_id=owner_id,
        shared_with_user_id=shared_with_user_id,
        permission=permission,
        expires_at=expires_at,
    )
    db.add(share)
    db.flush()
    return share


def get_share(db: Session, share_id: int) -> DeviceShare | None:
    return db.get(DeviceShare, share_id)


def get_share_for_user(db: Session, *, device_id: int, user_id: int) -> DeviceShare | None:
    stmt = select(DeviceShare).where(
        DeviceShare.device_id == device_id, DeviceShare.shared_with_user_id == user_id
    )
    return db.execute(stmt).scalar_one_or_none()


def list_shares_for_device(db: Session, device_id: int) -> list[DeviceShare]:
    stmt = select(DeviceShare).where(DeviceShare.device_id == device_id).order_by(DeviceShare.created_at)
    return list(db.execute(stmt).scalars())


def list_shares_received(db: Session, user_id: int) -> list[DeviceShare]:
    stmt = (
        select(DeviceShare)
        .where(DeviceShare.shared_with_user_id == user_id)
        .order_by(DeviceShare.created_at.desc())
    )
    return [s for s in db.execute(stmt).scalars() if s.is_active()]


def revoke_share(db: Session, share: DeviceShare) -> None:
    db.delete(share)
