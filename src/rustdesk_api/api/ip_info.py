"""WHOIS-style details for a public IP (/api/v1/ip-info).

Any authenticated user may ask: the data is public registry information, not
anything held about a device or user, so there is nothing to scope per owner.
The lookup itself is rate limited and cached (services/ip_info.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from rustdesk_api.api.deps import get_current_user, get_settings_dep
from rustdesk_api.config import Settings
from rustdesk_api.errors import ApiError
from rustdesk_api.models.user import User
from rustdesk_api.services import ip_info as ip_info_service

router = APIRouter(prefix="/api/v1/ip-info", tags=["ip-info"])


class IpInfoOut(BaseModel):
    ip: str
    # ok: details found. local: private/loopback/etc, never looked up.
    # disabled: IP_LOOKUP_ENABLED=false. unavailable: registry failed or rate limited.
    status: ip_info_service.Status
    network: str | None = None
    organization: str | None = None
    country: str | None = None
    cidr: str | None = None
    registry: str | None = None


def _get_service(request: Request, settings: Settings) -> ip_info_service.IpInfoService:
    service = getattr(request.app.state, "ip_info_service", None)
    if service is None:
        service = ip_info_service.IpInfoService(
            enabled=settings.ip_lookup_enabled,
            timeout=settings.ip_lookup_timeout_seconds,
            max_per_minute=settings.ip_lookup_max_per_minute,
        )
        request.app.state.ip_info_service = service
    return service


@router.get("", response_model=IpInfoOut)
def get_ip_info(
    request: Request,
    ip: str = Query(min_length=2, max_length=64),
    settings: Settings = Depends(get_settings_dep),
    _user: User = Depends(get_current_user),
) -> IpInfoOut:
    addr = ip_info_service.parse_ip(ip)
    if addr is None:
        raise ApiError("INVALID_IP", "Not a valid IP address.", 422)
    result = _get_service(request, settings).lookup(addr)
    info = result.info or ip_info_service.IpInfo()
    return IpInfoOut(
        ip=result.ip,
        status=result.status,
        network=info.network,
        organization=info.organization,
        country=info.country,
        cidr=info.cidr,
        registry=info.registry,
    )
