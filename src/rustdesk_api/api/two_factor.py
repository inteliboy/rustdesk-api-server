"""Two-factor authentication management (/api/v1/auth/2fa/...).

The sign-in half lives in api/auth.py (the WebUI's `/login` + `/login/2fa` and
the RustDesk client's two-step `/api/login`). Everything here needs a signed-in
session, and anything that weakens the account (turning 2FA off, minting new
recovery codes) asks for the password *and* a current code, so a stolen session
alone cannot strip the second factor.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import (
    enforce_auth_rate_limit,
    get_client_ip,
    get_interactive_user,
    get_settings_dep,
    verify_csrf,
)
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.user import User
from rustdesk_api.security import totp
from rustdesk_api.security.encryption import SecretBox, get_secret_box
from rustdesk_api.security.passwords import verify_password
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import two_factor as two_factor_service

router = APIRouter(prefix="/api/v1/auth/2fa", tags=["two-factor"])

ISSUER = "RustDesk API Server"


class TwoFactorStatus(BaseModel):
    enabled: bool
    # False when the server has no DATA_ENCRYPTION_KEY: it could not store the secret.
    available: bool
    recovery_codes_remaining: int = 0


class SetupOut(BaseModel):
    secret: str
    otpauth_uri: str


class CodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class ConfirmRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=64)


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]


def _box(settings: Settings) -> SecretBox | None:
    return get_secret_box(settings.data_encryption_key)


def _translate(exc: two_factor_service.TwoFactorError) -> ApiError:
    if isinstance(exc, two_factor_service.NotAvailable):
        return ApiError("TWO_FACTOR_UNAVAILABLE", str(exc), 409)
    if isinstance(exc, two_factor_service.AlreadyEnabled):
        return ApiError("TWO_FACTOR_ALREADY_ON", str(exc), 409)
    if isinstance(exc, two_factor_service.BadCode):
        return ApiError("INVALID_CODE", str(exc), 422)
    return ApiError("TWO_FACTOR_NOT_ON", str(exc), 409)


def _require_password_and_code(
    db: Session, user: User, settings: Settings, payload: ConfirmRequest, client_ip: str | None
) -> None:
    """Both must be right. Failures are audited and answered alike, so the reply
    does not say which half was wrong."""
    # The password first: a wrong one must not spend a code.
    if verify_password(payload.password, user.password_hash) and two_factor_service.check_code(
        db, user, _box(settings), payload.code
    ):
        return
    audit_service.record(
        db,
        action="two_factor_check",
        actor_id=user.id,
        result="failure",
        ip_address=client_ip,
        detail={"via": "webui"},
    )
    db.commit()
    raise ApiError("INVALID_CREDENTIALS", "The password or the code is not right.", 403)


@router.get("", response_model=TwoFactorStatus)
def status(
    db: Session = Depends(get_db),
    user: User = Depends(get_interactive_user),
    settings: Settings = Depends(get_settings_dep),
) -> TwoFactorStatus:
    return TwoFactorStatus(
        enabled=user.totp_enabled,
        available=_box(settings) is not None,
        recovery_codes_remaining=(
            two_factor_service.recovery_codes_remaining(db, user) if user.totp_enabled else 0
        ),
    )


@router.post(
    "/setup", response_model=SetupOut, dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)]
)
def begin_setup(
    db: Session = Depends(get_db),
    user: User = Depends(get_interactive_user),
    settings: Settings = Depends(get_settings_dep),
) -> SetupOut:
    """A new secret to add to an authenticator app. Not active until `/enable`."""
    try:
        secret = two_factor_service.begin_setup(db, user, _box(settings))
    except two_factor_service.TwoFactorError as exc:
        raise _translate(exc) from exc
    db.commit()
    return SetupOut(secret=secret, otpauth_uri=totp.provisioning_uri(secret, user.username, ISSUER))


@router.post(
    "/enable",
    response_model=RecoveryCodesOut,
    dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)],
)
def enable(
    payload: CodeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_interactive_user),
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> RecoveryCodesOut:
    try:
        codes = two_factor_service.enable(db, user, _box(settings), payload.code)
    except two_factor_service.TwoFactorError as exc:
        raise _translate(exc) from exc
    audit_service.record(
        db,
        action="two_factor_enabled",
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        ip_address=client_ip,
    )
    db.commit()
    return RecoveryCodesOut(recovery_codes=codes)


@router.post(
    "/disable", status_code=204, dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)]
)
def disable(
    payload: ConfirmRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_interactive_user),
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> None:
    if not user.totp_enabled:
        raise ApiError("TWO_FACTOR_NOT_ON", "Two-factor authentication is not on.", 409)
    _require_password_and_code(db, user, settings, payload, client_ip)
    two_factor_service.disable(db, user)
    audit_service.record(
        db,
        action="two_factor_disabled",
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        ip_address=client_ip,
    )
    db.commit()


@router.post(
    "/recovery-codes",
    response_model=RecoveryCodesOut,
    dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)],
)
def regenerate_recovery_codes(
    payload: ConfirmRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_interactive_user),
    settings: Settings = Depends(get_settings_dep),
    client_ip: str | None = Depends(get_client_ip),
) -> RecoveryCodesOut:
    if not user.totp_enabled:
        raise ApiError("TWO_FACTOR_NOT_ON", "Two-factor authentication is not on.", 409)
    _require_password_and_code(db, user, settings, payload, client_ip)
    codes = two_factor_service.regenerate_recovery_codes(db, user)
    audit_service.record(
        db,
        action="recovery_codes_regenerated",
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        ip_address=client_ip,
    )
    db.commit()
    return RecoveryCodesOut(recovery_codes=codes)
