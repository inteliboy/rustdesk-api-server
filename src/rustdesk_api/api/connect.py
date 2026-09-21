"""Helper for setting up RustDesk clients (/api/v1/connect): the server settings as the
client's config string, a QR code for it and a ready-made file name.

Any signed-in user may use it. What it shows is what every client needs to know
anyway - the ID and relay servers, the API server address and the *public* key of the
ID server - and nothing here is a credential (an enrollment token is created on the
Security page and only ever mentioned as a placeholder)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from rustdesk_api.api.deps import get_current_user, get_settings_dep, verify_csrf
from rustdesk_api.config import Settings
from rustdesk_api.models.user import User
from rustdesk_api.security import qr
from rustdesk_api.services import client_config

router = APIRouter(prefix="/api/v1/connect", tags=["connect"])


class ServersIn(BaseModel):
    id_server: str = Field(min_length=1, max_length=client_config.MAX_FIELD)
    relay_server: str = Field(default="", max_length=client_config.MAX_FIELD)
    api_server: str = Field(default="", max_length=client_config.MAX_FIELD)
    key: str = Field(default="", max_length=client_config.MAX_KEY)


class ServersOut(BaseModel):
    id_server: str
    relay_server: str
    api_server: str
    key: str


class ConfigOut(BaseModel):
    config_string: str
    exe_name: str
    # An SVG data: URI of the config string, for the mobile clients' "scan" button.
    qr_svg: str
    # Codes for settings that are accepted but will not behave as expected.
    warnings: list[str]


@router.get("", response_model=ServersOut)
def defaults(_user: User = Depends(get_current_user), settings: Settings = Depends(get_settings_dep)):
    """The values configured for this server (RUSTDESK_ID_SERVER, RUSTDESK_RELAY_SERVER,
    RUSTDESK_KEY and EXTERNAL_URL); the page lets the person adjust them."""
    return ServersOut(
        id_server=settings.rustdesk_id_server,
        relay_server=settings.rustdesk_relay_server,
        api_server=settings.external_url.rstrip("/"),
        key=settings.rustdesk_key,
    )


@router.post("/config", response_model=ConfigOut, dependencies=[Depends(verify_csrf)])
def build_config(payload: ServersIn, _user: User = Depends(get_current_user)) -> ConfigOut:
    servers = client_config.ClientServers(
        id_server=client_config.clean(payload.id_server),
        relay_server=client_config.clean(payload.relay_server),
        api_server=client_config.clean(payload.api_server).rstrip("/"),
        key=client_config.clean(payload.key, client_config.MAX_KEY),
    )
    text = client_config.config_string(servers)
    return ConfigOut(
        config_string=text,
        exe_name=client_config.exe_name(servers),
        qr_svg=qr.svg_data_uri(text),
        warnings=client_config.warnings(servers),
    )
