from rustdesk_api.db.database import get_session_factory
from rustdesk_api.services import authentication as auth_service
from rustdesk_api.services import devices as device_service


def test_register_or_update_is_idempotent(app):
    """Given a device heartbeat/sysinfo report, when the same rustdesk_id is
    reported twice, then only one device record exists (CLAUDE.md section 43)."""
    Session = get_session_factory()
    with Session() as db:
        device_service.register_or_update(db, rustdesk_id="abc123", hostname="host-a")
        device_service.register_or_update(db, rustdesk_id="abc123", hostname="host-a-renamed")
        db.commit()

        items, total = device_service.list_devices(db, owner_id=None)
        assert total == 1
        assert items[0].hostname == "host-a-renamed"


def test_register_or_update_claims_unowned_device_but_not_owned_one(app):
    Session = get_session_factory()
    with Session() as db:
        user_a = auth_service.create_user(db, username="user-a", password="passwordpassword1")
        user_b = auth_service.create_user(db, username="user-b", password="passwordpassword1")
        db.commit()

        device_service.register_or_update(db, rustdesk_id="dev1", owner_id=user_a.id)
        db.commit()
        device = device_service.get_by_rustdesk_id(db, "dev1")
        assert device.owner_id == user_a.id

        # A different user's report should not steal ownership.
        device_service.register_or_update(db, rustdesk_id="dev1", owner_id=user_b.id)
        db.commit()
        device = device_service.get_by_rustdesk_id(db, "dev1")
        assert device.owner_id == user_a.id


def test_touch_heartbeat_does_not_create_new_device(app):
    Session = get_session_factory()
    with Session() as db:
        result = device_service.touch_heartbeat(db, rustdesk_id="unknown-device")
        db.commit()
        assert result is None
        items, total = device_service.list_devices(db, owner_id=None)
        assert total == 0
