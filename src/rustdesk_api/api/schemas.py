"""Pydantic request/response models for the project's own management API
(/api/v1/...). RustDesk-protocol endpoints under /api/... intentionally use
plain dicts instead, so their wire format matches the client exactly rather
than being reshaped to look RESTful (CLAUDE.md section 8)."""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None
    is_active: bool
    is_admin: bool
    two_factor_enabled: bool = False
    # Set while the account is locked by too many wrong passwords.
    locked_until: datetime.datetime | None = None
    created_at: datetime.datetime
    last_login_at: datetime.datetime | None


class LoginResponse(BaseModel):
    # A finished login carries a token and the user. An account with 2FA gets
    # `two_factor_required` and a `challenge` instead, to send to
    # /api/v1/auth/login/2fa together with a code.
    access_token: str | None = None
    csrf_token: str | None = None
    user: UserOut | None = None
    two_factor_required: bool = False
    challenge: str | None = None


class TwoFactorLoginRequest(BaseModel):
    challenge: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=64)


class SetupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=150)
    password: str = Field(min_length=8, max_length=256)
    email: str | None = None


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=150)
    password: str = Field(min_length=8, max_length=256)
    email: str | None = None
    is_admin: bool = False


class UpdateUserRequest(BaseModel):
    is_active: bool | None = None
    is_admin: bool | None = None
    email: str | None = None


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    color: str


class DeviceOut(BaseModel):
    id: int
    rustdesk_id: str
    name: str | None
    hostname: str | None
    alias: str | None
    username: str | None
    platform: str | None
    os_version: str | None
    client_version: str | None
    ip_address: str | None
    # "http" or "https": how the client's last heartbeat reached this server.
    api_scheme: str | None = None
    cpu: str | None
    memory: str | None
    note: str | None = None
    last_seen: datetime.datetime | None
    owner_id: int | None
    owner_username: str | None = None
    group_id: int | None
    group_name: str | None = None
    # The strategy set on this device itself, and the one that applies to it
    # (its own, else its group's). Names only; the strategies are admin-managed.
    strategy_id: int | None = None
    strategy_name: str | None = None
    effective_strategy_name: str | None = None
    # How many incoming connections the client reported at its last heartbeat.
    connection_count: int = 0
    # A different install than the one on record uploaded its system info and is
    # waiting for the owner or an administrator to accept it. (The device `uuid`
    # itself is never returned: it is what authenticates the device's heartbeat.)
    uuid_change_pending: bool = False
    uuid_change_at: datetime.datetime | None = None
    uuid_change_ip: str | None = None
    tags: list[TagOut] = []
    created_at: datetime.datetime
    updated_at: datetime.datetime
    online: bool = False
    # An administrator asked to be notified when this device goes offline.
    watch_offline: bool = False
    # Silent for DEVICE_STALE_DAYS: hidden from the default list until it reports again.
    archived: bool = False
    # Older than MIN_CLIENT_VERSION.
    outdated: bool = False


class DeviceListResponse(BaseModel):
    items: list[DeviceOut]
    page: int
    page_size: int
    total: int


class UpdateDeviceRequest(BaseModel):
    alias: str | None = None
    name: str | None = None
    note: str | None = Field(default=None, max_length=500)
    owner_id: int | None = None
    group_id: int | None = None
    tag_ids: list[int] | None = None


class DashboardStats(BaseModel):
    total_devices: int
    online_devices: int
    offline_devices: int
    total_users: int
    total_groups: int
    total_tags: int
    # Registered in the last 24 hours (archived devices excluded like everywhere here).
    new_devices_24h: int = 0
    # Client older than MIN_CLIENT_VERSION (0 when that is not set).
    outdated_devices: int = 0
    archived_devices: int = 0
    # Devices that would receive no strategy at all: nothing of their own, none through
    # their group, no default. Worth a look once strategies are in use.
    devices_without_strategy: int = 0


class GroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    owner_id: int
    created_at: datetime.datetime
    updated_at: datetime.datetime
    device_count: int = 0
    strategy_id: int | None = None
    strategy_name: str | None = None


class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    description: str | None = None


class UpdateGroupRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = None


# Rendered into a style attribute by the WebUI, so only #rrggbb is accepted.
TAG_COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"


class CreateTagRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    color: str | None = Field(default=None, pattern=TAG_COLOR_PATTERN)


class UpdateTagRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    color: str | None = Field(default=None, pattern=TAG_COLOR_PATTERN)


class DeviceShareOut(BaseModel):
    id: int
    device_id: int
    owner_id: int
    shared_with_user_id: int
    shared_with_username: str
    permission: str
    created_at: datetime.datetime
    expires_at: datetime.datetime | None


class CreateShareRequest(BaseModel):
    username: str
    permission: str = "view"
    expires_at: datetime.datetime | None = None


class AddressBookTagOut(BaseModel):
    name: str
    # `#rrggbb` for the WebUI; the client's own value is an ARGB integer.
    color: str


class AddressBookEntryOut(BaseModel):
    id: int
    rustdesk_id: str
    username: str | None
    hostname: str | None
    platform: str | None
    alias: str | None
    note: str | None = None
    tags: list[AddressBookTagOut] = []
    # Never the raw value (CLAUDE.md section 18) - just whether the client
    # has synced a connection password for this entry, matching the
    # reference project's own has_rhash display (never rhash itself).
    has_password: bool = False
    # Set (by the list endpoint only) when a device with this RustDesk ID is
    # registered AND the caller may view it - so it can be linked to without
    # confirming to anyone else that the ID exists.
    device_id: int | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class CreateAddressBookEntryRequest(BaseModel):
    rustdesk_id: str = Field(min_length=1, max_length=64)
    alias: str | None = None
    hostname: str | None = None
    platform: str | None = None
    note: str | None = None
    tags: list[str] = []


class UpdateAddressBookEntryRequest(BaseModel):
    alias: str | None = None
    hostname: str | None = None
    platform: str | None = None
    note: str | None = None
    tags: list[str] = []


class AddressBookShareOut(BaseModel):
    user_id: int
    username: str
    rule: int


class AddressBookOut(BaseModel):
    guid: str
    name: str
    note: str | None
    owner: str
    is_personal: bool
    # The caller's own rule: 1 read, 2 read/write, 3 full control (the owner).
    rule: int
    can_write: bool
    is_owner: bool
    entry_count: int
    # Only filled in for the owner.
    shares: list[AddressBookShareOut] = []


class CreateAddressBookRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=500)


class UpdateAddressBookRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=500)


class SetAddressBookShareRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    rule: int = Field(ge=1, le=3)
