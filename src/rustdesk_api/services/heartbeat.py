from __future__ import annotations

from sqlalchemy.orm import Session

from rustdesk_api.models.device import Device
from rustdesk_api.services import devices as device_service


def handle_heartbeat(
    db: Session,
    *,
    rustdesk_id: str,
    uuid: str | None,
    ip_address: str | None,
    online_timeout: int | None = None,
) -> Device | None:
    """RustDesk clients send periodic heartbeats keyed by id/uuid.

    Unknown devices are not implicitly created here - registration happens
    via sysinfo (see services.devices.register_or_update). A heartbeat for
    an unknown id simply updates nothing and returns None.
    """
    device = device_service.get_by_rustdesk_id(db, rustdesk_id)
    if device is None:
        return None
    return device_service.touch_heartbeat(
        db, rustdesk_id=rustdesk_id, ip_address=ip_address, online_timeout=online_timeout
    )
