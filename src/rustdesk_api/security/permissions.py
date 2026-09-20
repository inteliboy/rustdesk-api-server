"""Pure authorization logic, independent of the web framework.

Kept separate from FastAPI dependencies (see api/deps.py) so authorization
rules can be unit tested without spinning up HTTP requests, and so the same
rules apply consistently everywhere (CLAUDE.md section 12: never rely on
the WebUI to enforce security).
"""

from __future__ import annotations

from rustdesk_api.models.device import Device
from rustdesk_api.models.user import User


class PermissionDenied(Exception):
    pass


def require_admin(user: User) -> None:
    if not user.is_admin:
        raise PermissionDenied("Administrator privileges are required.")


def _active_share_permission(user: User, device: Device) -> str | None:
    """Returns the strongest active share permission this user holds on
    this device, or None. Reads device.shares (an ORM relationship - a
    cheap lazy-load within the caller's existing session, not a fresh
    query the caller has to wire up separately)."""
    for share in device.shares:
        if share.shared_with_user_id == user.id and share.is_active():
            return share.permission
    return None


def can_view_device(user: User, device: Device) -> bool:
    if user.is_admin:
        return True
    if device.owner_id == user.id:
        return True
    return _active_share_permission(user, device) is not None


def can_edit_device(user: User, device: Device) -> bool:
    """Alias/name edits, and attaching/detaching tags. Available to the
    owner, an admin, or a share with "control" permission."""
    if user.is_admin:
        return True
    if device.owner_id == user.id:
        return True
    return _active_share_permission(user, device) == "control"


def can_delete_device(user: User, device: Device) -> bool:
    """Delete or reassign ownership/group. Never available through a share,
    regardless of permission level - only the owner or an admin."""
    if user.is_admin:
        return True
    return device.owner_id == user.id


def can_manage_shares(user: User, device: Device) -> bool:
    """Create/revoke shares for a device. Only the owner or an admin - a
    shared user, even with "control", cannot re-share the device further."""
    if user.is_admin:
        return True
    return device.owner_id == user.id


def can_manage_connections(user: User, device: Device) -> bool:
    """End a live incoming session on a device. Owner or admin only: a share,
    even with "control", is about editing the record, not about cutting off
    whoever is using the machine."""
    if user.is_admin:
        return True
    return device.owner_id == user.id
