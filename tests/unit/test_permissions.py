import datetime

from rustdesk_api.models.device import Device
from rustdesk_api.models.role import Role
from rustdesk_api.models.share import DeviceShare
from rustdesk_api.models.user import User
from rustdesk_api.models.user_group import UserGroup
from rustdesk_api.security.permissions import (
    can_delete_device,
    can_edit_device,
    can_manage_shares,
    can_view_device,
    effective_permissions,
    has_permission,
    requires_2fa_by_role,
)


def _user(id_: int, is_admin: bool = False) -> User:
    return User(id=id_, username=f"user{id_}", password_hash="x", is_admin=is_admin)


def _device(owner_id: int | None, shares: list[DeviceShare] | None = None) -> Device:
    device = Device(id=1, rustdesk_id="abc", owner_id=owner_id)
    if shares:
        device.shares = shares
    return device


def _share(shared_with_user_id: int, permission: str, expires_at=None) -> DeviceShare:
    return DeviceShare(
        device_id=1,
        owner_id=1,
        shared_with_user_id=shared_with_user_id,
        permission=permission,
        expires_at=expires_at,
    )


def test_owner_can_view_edit_and_delete_their_device():
    owner = _user(1)
    device = _device(owner_id=1)
    assert can_view_device(owner, device) is True
    assert can_edit_device(owner, device) is True
    assert can_delete_device(owner, device) is True
    assert can_manage_shares(owner, device) is True


def test_other_user_cannot_view_edit_or_delete_device():
    """Given a device owned by user A, when user B (not an admin, no share)
    checks permissions on it, then access is denied - this is the core
    IDOR protection (CLAUDE.md section 65)."""
    owner = _user(1)
    other = _user(2)
    device = _device(owner_id=owner.id)
    assert can_view_device(other, device) is False
    assert can_edit_device(other, device) is False
    assert can_delete_device(other, device) is False
    assert can_manage_shares(other, device) is False


def test_admin_can_view_edit_and_delete_any_device():
    admin = _user(99, is_admin=True)
    device = _device(owner_id=1)
    assert can_view_device(admin, device) is True
    assert can_edit_device(admin, device) is True
    assert can_delete_device(admin, device) is True


def test_unowned_device_is_not_visible_to_non_admin():
    user = _user(1)
    device = _device(owner_id=None)
    assert can_view_device(user, device) is False


def test_view_share_grants_view_but_not_edit_or_delete():
    other = _user(2)
    device = _device(owner_id=1, shares=[_share(shared_with_user_id=2, permission="view")])
    assert can_view_device(other, device) is True
    assert can_edit_device(other, device) is False
    assert can_delete_device(other, device) is False
    assert can_manage_shares(other, device) is False


def test_control_share_grants_view_and_edit_but_not_delete_or_manage_shares():
    other = _user(2)
    device = _device(owner_id=1, shares=[_share(shared_with_user_id=2, permission="control")])
    assert can_view_device(other, device) is True
    assert can_edit_device(other, device) is True
    assert can_delete_device(other, device) is False
    assert can_manage_shares(other, device) is False


def test_expired_share_grants_nothing():
    other = _user(2)
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    device = _device(
        owner_id=1, shares=[_share(shared_with_user_id=2, permission="control", expires_at=past)]
    )
    assert can_view_device(other, device) is False
    assert can_edit_device(other, device) is False


def test_share_for_a_different_user_does_not_grant_access():
    unrelated = _user(3)
    device = _device(owner_id=1, shares=[_share(shared_with_user_id=2, permission="control")])
    assert can_view_device(unrelated, device) is False


def test_only_the_owner_or_an_admin_can_end_connections_even_with_a_control_share():
    from rustdesk_api.security.permissions import can_manage_connections

    device = _device(owner_id=1, shares=[_share(3, "control")])
    assert can_manage_connections(_user(1), device) is True
    assert can_manage_connections(_user(9, is_admin=True), device) is True
    assert (
        can_manage_connections(_user(3), device) is False
    )  # a "control" share edits, it does not disconnect
    assert can_manage_connections(_user(4), device) is False
    assert can_manage_connections(_user(4), _device(owner_id=None)) is False


def _role(name: str, permissions: dict[str, str], requires_2fa: bool = False) -> Role:
    role = Role(id=1, name=name, requires_2fa=requires_2fa)
    role.permissions = permissions
    return role


def test_a_user_with_no_role_has_no_permissions():
    user = _user(1)
    user.user_groups = []
    assert effective_permissions(user) == {}
    assert has_permission(user, "devices", "view") is False


def test_an_admin_has_every_permission_without_needing_a_role():
    admin = _user(1, is_admin=True)
    admin.user_groups = []
    assert has_permission(admin, "devices", "manage") is True
    assert has_permission(admin, "settings", "manage") is True


def test_a_direct_role_grants_its_permissions():
    user = _user(1)
    user.role = _role("Support", {"devices": "view", "users": "manage"})
    user.user_groups = []
    assert effective_permissions(user) == {"devices": "view", "users": "manage"}
    assert has_permission(user, "devices", "view") is True
    assert has_permission(user, "devices", "manage") is False
    assert has_permission(user, "users", "manage") is True
    assert has_permission(user, "logs", "view") is False


def test_a_role_via_a_user_group_grants_its_permissions():
    user = _user(1)
    user.role = None
    group = UserGroup(id=1, name="Support team")
    group.role = _role("Support", {"logs": "view"})
    user.user_groups = [group]
    assert has_permission(user, "logs", "view") is True
    assert has_permission(user, "logs", "manage") is False


def test_multiple_roles_combine_by_taking_the_highest_level_per_area():
    user = _user(1)
    user.role = _role("Viewer", {"devices": "view"})
    group = UserGroup(id=1, name="Managers")
    group.role = _role("Manager", {"devices": "manage"})
    user.user_groups = [group]
    assert effective_permissions(user)["devices"] == "manage"


def test_a_user_group_with_no_role_grants_nothing():
    user = _user(1)
    user.role = None
    group = UserGroup(id=1, name="No role yet")
    group.role = None
    user.user_groups = [group]
    assert effective_permissions(user) == {}


def test_requires_2fa_by_role_checks_direct_and_group_roles():
    plain = _user(1)
    plain.role = _role("Plain", {})
    plain.user_groups = []
    assert requires_2fa_by_role(plain) is False

    direct = _user(2)
    direct.role = _role("Sensitive", {}, requires_2fa=True)
    direct.user_groups = []
    assert requires_2fa_by_role(direct) is True

    via_group = _user(3)
    via_group.role = None
    group = UserGroup(id=1, name="Sensitive team")
    group.role = _role("Sensitive", {}, requires_2fa=True)
    via_group.user_groups = [group]
    assert requires_2fa_by_role(via_group) is True
