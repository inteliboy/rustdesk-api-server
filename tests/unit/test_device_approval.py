"""Who may see a device that is waiting for approval, and forgetting stale ones."""

from __future__ import annotations

import datetime

import pytest

from rustdesk_api.models.device import Device
from rustdesk_api.models.user import User
from rustdesk_api.security import permissions
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import fleet


def _user(user_id: int, *, admin: bool = False) -> User:
    return User(id=user_id, username=f"u{user_id}", password_hash="x", is_admin=admin, is_active=True)


@pytest.mark.parametrize("approval", ["pending", "rejected"])
def test_only_an_administrator_has_anything_to_do_with_an_unapproved_device(approval):
    device = Device(rustdesk_id="1", owner_id=2, approval=approval)
    owner, admin = _user(2), _user(1, admin=True)
    for check in (
        permissions.can_view_device,
        permissions.can_edit_device,
        permissions.can_delete_device,
        permissions.can_manage_shares,
        permissions.can_manage_connections,
    ):
        assert check(owner, device) is False, check.__name__
        assert check(admin, device) is True, check.__name__


def test_the_owner_of_an_approved_device_is_unaffected():
    device = Device(rustdesk_id="1", owner_id=2, approval="approved")
    assert permissions.can_view_device(_user(2), device) is True
    assert permissions.can_edit_device(_user(2), device) is True


def test_only_an_active_administrator_vouches():
    assert device_service.vouches(None) is False
    assert device_service.vouches(_user(1)) is False
    assert device_service.vouches(_user(1, admin=True)) is True
    gone = _user(1, admin=True)
    gone.is_active = False
    assert device_service.vouches(gone) is False


def test_stale_waiting_devices_are_dropped_and_rejected_ones_kept(settings, app):
    from rustdesk_api.db.database import get_session_factory

    settings = settings.model_copy(update={"device_stale_days": 7})
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
    with get_session_factory()() as db:
        db.add_all(
            [
                Device(rustdesk_id="old-pending", approval="pending", last_seen=old),
                Device(
                    rustdesk_id="fresh-pending",
                    approval="pending",
                    last_seen=datetime.datetime.now(datetime.timezone.utc),
                ),
                Device(rustdesk_id="old-rejected", approval="rejected", last_seen=old),
                Device(rustdesk_id="old-approved", approval="approved", last_seen=old),
            ]
        )
        db.commit()

        assert fleet.drop_stale_pending(db, settings) == 1
        left = sorted(d.rustdesk_id for d in db.query(Device))
        assert left == ["fresh-pending", "old-approved", "old-rejected"]

        # Archiving leaves waiting devices alone (they have a list of their own).
        db.add(Device(rustdesk_id="old-pending-2", approval="pending", last_seen=old))
        db.commit()
        fleet.archive_stale(db, settings)
        assert db.query(Device).filter_by(rustdesk_id="old-pending-2").one().archived_at is None
        assert db.query(Device).filter_by(rustdesk_id="old-approved").one().archived_at is not None


def test_no_days_means_nothing_is_dropped(settings, app):
    from rustdesk_api.db.database import get_session_factory

    with get_session_factory()() as db:
        db.add(Device(rustdesk_id="p", approval="pending", last_seen=None))
        db.commit()
        assert fleet.drop_stale_pending(db, settings.model_copy(update={"device_stale_days": 0})) == 0
