"""Pure authorization logic, independent of the web framework.

Kept separate from FastAPI dependencies (see api/deps.py) so authorization
rules can be unit tested without spinning up HTTP requests, and so the same
rules apply consistently everywhere (CLAUDE.md section 12: never rely on
the WebUI to enforce security).
"""

from __future__ import annotations

from rustdesk_api.models.device import Device
from rustdesk_api.models.role import PERMISSION_LEVELS
from rustdesk_api.models.user import User


class PermissionDenied(Exception):
    pass


def require_admin(user: User) -> None:
    if not user.is_admin:
        raise PermissionDenied("Administrator privileges are required.")


# --- Role permission matrix (CortenDesk-style) -----------------------------------
#
# A role never changes which *devices* a user can see or act on - that is entirely
# the ownership/share logic below. It only grants a non-admin user partial access
# to console areas that would otherwise require is_admin (users, settings, ...).
# is_admin remains a full, unconditional bypass, checked by callers (has_permission),
# not folded into effective_permissions, so effective_permissions stays a pure
# "what did roles grant" computation that is easy to unit test on its own.

_LEVEL_RANK = {level: rank for rank, level in enumerate(PERMISSION_LEVELS)}


def effective_permissions(user: User) -> dict[str, str]:
    """The highest level per area granted by the user's direct role and every
    role reachable through a user group they belong to."""
    best: dict[str, str] = {}
    roles = []
    if user.role is not None:
        roles.append(user.role)
    for group in user.user_groups:
        if group.role is not None:
            roles.append(group.role)
    for role in roles:
        for area, level in role.permissions.items():
            if _LEVEL_RANK.get(level, 0) > _LEVEL_RANK.get(best.get(area, "none"), 0):
                best[area] = level
    return best


def requires_2fa_by_role(user: User) -> bool:
    """True if any role the user holds (directly or through a user group) demands
    two-factor authentication of everyone assigned to it."""
    if user.role is not None and user.role.requires_2fa:
        return True
    return any(group.role is not None and group.role.requires_2fa for group in user.user_groups)


def has_permission(user: User, area: str, level: str) -> bool:
    """True if `user` may act at `level` (or higher) in `area`: either they are an
    administrator (a full bypass), or a role grants it."""
    if user.is_admin:
        return True
    granted = effective_permissions(user).get(area, "none")
    return _LEVEL_RANK.get(granted, 0) >= _LEVEL_RANK.get(level, 0)


def _active_share_permission(user: User, device: Device) -> str | None:
    """Returns the strongest active share permission this user holds on
    this device, or None. Reads device.shares (an ORM relationship - a
    cheap lazy-load within the caller's existing session, not a fresh
    query the caller has to wire up separately)."""
    for share in device.shares:
        if share.shared_with_user_id == user.id and share.is_active():
            return share.permission
    return None


def _not_yet_approved(user: User, device: Device) -> bool:
    """A device waiting for (or refused) an administrator's decision belongs to nobody
    else: not even to the user it was registered under."""
    return not user.is_admin and not device.is_approved


def can_view_device(user: User, device: Device) -> bool:
    if user.is_admin:
        return True
    if _not_yet_approved(user, device):
        return False
    if device.owner_id == user.id:
        return True
    return _active_share_permission(user, device) is not None


def can_edit_device(user: User, device: Device) -> bool:
    """Alias/name edits, and attaching/detaching tags. Available to the
    owner, an admin, or a share with "control" permission."""
    if user.is_admin:
        return True
    if _not_yet_approved(user, device):
        return False
    if device.owner_id == user.id:
        return True
    return _active_share_permission(user, device) == "control"


def can_delete_device(user: User, device: Device) -> bool:
    """Delete or reassign ownership/group. Never available through a share,
    regardless of permission level - only the owner or an admin."""
    if user.is_admin:
        return True
    if _not_yet_approved(user, device):
        return False
    return device.owner_id == user.id


def can_manage_shares(user: User, device: Device) -> bool:
    """Create/revoke shares for a device. Only the owner or an admin - a
    shared user, even with "control", cannot re-share the device further."""
    if user.is_admin:
        return True
    if _not_yet_approved(user, device):
        return False
    return device.owner_id == user.id


def can_manage_connections(user: User, device: Device) -> bool:
    """End a live incoming session on a device. Owner or admin only: a share,
    even with "control", is about editing the record, not about cutting off
    whoever is using the machine."""
    if user.is_admin:
        return True
    if _not_yet_approved(user, device):
        return False
    return device.owner_id == user.id
