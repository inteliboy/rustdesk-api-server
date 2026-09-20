"""Device sharing (/api/v1/devices/{device_id}/shares/...).

Only a device's owner or an admin can view/create/revoke shares for it
(CLAUDE.md section 65 - IDOR: this must not leak share details for a
device the caller doesn't own just because the device id is guessable).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_current_user, verify_csrf
from rustdesk_api.api.schemas import CreateShareRequest, DeviceShareOut
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.device import Device
from rustdesk_api.models.share import DeviceShare
from rustdesk_api.models.user import User
from rustdesk_api.security.permissions import can_manage_shares
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import authentication as auth_service
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import sharing as sharing_service

router = APIRouter(prefix="/api/v1/devices/{device_id}/shares", tags=["shares"])


def _to_out(share: DeviceShare) -> DeviceShareOut:
    return DeviceShareOut(
        id=share.id,
        device_id=share.device_id,
        owner_id=share.owner_id,
        shared_with_user_id=share.shared_with_user_id,
        shared_with_username=share.shared_with.username,
        permission=share.permission,
        created_at=share.created_at,
        expires_at=share.expires_at,
    )


def _get_manageable_device_or_404(db: Session, device_id: int, user: User) -> Device:
    device = device_service.get_by_id(db, device_id)
    # Same 404 whether the device doesn't exist or the caller can't manage
    # its shares - never confirm existence of a device the caller can't see.
    if device is None or not can_manage_shares(user, device):
        raise ApiError("DEVICE_NOT_FOUND", "The requested device does not exist.", 404)
    return device


@router.get("", response_model=list[DeviceShareOut])
def list_shares(
    device_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[DeviceShareOut]:
    _get_manageable_device_or_404(db, device_id, user)
    return [_to_out(s) for s in sharing_service.list_shares_for_device(db, device_id)]


@router.post("", response_model=DeviceShareOut, status_code=201, dependencies=[Depends(verify_csrf)])
def create_share(
    device_id: int,
    payload: CreateShareRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DeviceShareOut:
    device = _get_manageable_device_or_404(db, device_id, user)

    target = auth_service.get_user_by_username(db, payload.username)
    if target is None:
        raise ApiError("USER_NOT_FOUND", "No user with that username exists.", 404)

    try:
        share = sharing_service.create_share(
            db,
            device=device,
            owner_id=user.id,
            shared_with_user_id=target.id,
            permission=payload.permission,
            expires_at=payload.expires_at,
        )
    except sharing_service.InvalidPermission as exc:
        raise ApiError("INVALID_PERMISSION", str(exc), 400) from exc
    except sharing_service.AlreadyShared as exc:
        raise ApiError("ALREADY_SHARED", str(exc), 409) from exc

    audit_service.record(
        db,
        action="device_shared",
        actor_id=user.id,
        target_type="device",
        target_id=device.id,
        detail={"shared_with": target.username, "permission": payload.permission},
    )
    db.commit()
    return _to_out(share)


@router.delete("/{share_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def revoke_share(
    device_id: int,
    share_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    device = _get_manageable_device_or_404(db, device_id, user)
    share = sharing_service.get_share(db, share_id)
    if share is None or share.device_id != device.id:
        raise ApiError("SHARE_NOT_FOUND", "The requested share does not exist.", 404)

    unshared_with = share.shared_with.username
    sharing_service.revoke_share(db, share)
    audit_service.record(
        db,
        action="device_unshared",
        actor_id=user.id,
        target_type="device",
        target_id=device.id,
        detail={"shared_with": unshared_with},
    )
    db.commit()
