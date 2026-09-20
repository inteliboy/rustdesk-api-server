import datetime

from rustdesk_api.models.device import Device
from rustdesk_api.models.share import DeviceShare
from rustdesk_api.models.user import User
from rustdesk_api.security.permissions import (
    can_delete_device,
    can_edit_device,
    can_manage_shares,
    can_view_device,
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
