"""Windows setup files for this server (/api/v1/admin/installer): pick a RustDesk release
(stable or nightly) and architecture, then either build the .exe on the server or download
a kit that builds and signs it on another machine.

Administrators only. Building runs a program on the server, downloads from GitHub and
may sign with the server's certificate, so it is not something every signed-in user can
trigger. The signing command is an environment variable (INSTALLER_SIGN_COMMAND) and is
never shown."""

from __future__ import annotations

import base64
import binascii
import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from rustdesk_api.api.connect import ServersIn
from rustdesk_api.api.deps import (
    enforce_auth_rate_limit,
    get_current_admin,
    get_settings_dep,
    require_permission,
    verify_csrf,
)
from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.user import User
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import client_config
from rustdesk_api.services import installer as installer_service
from rustdesk_api.services import signing as signing_service

router = APIRouter(prefix="/api/v1/admin/installer", tags=["installer"])

Arch = Literal["x64", "arm64"]


class CertificateOut(BaseModel):
    """What may be shown about the uploaded certificate: never the key, the file or a password."""

    subject: str
    issuer: str
    thumbprint: str
    not_before: datetime.datetime
    not_after: datetime.datetime
    expired: bool
    code_signing: bool
    uploaded_by: str
    uploaded_at: datetime.datetime


class StatusOut(BaseModel):
    available: bool
    # 'disabled' (INSTALLER_BUILD_ENABLED=false) or 'no_makensis'; null when it can build.
    reason: str | None
    # INSTALLER_SIGN_COMMAND is set: the server signs with that (and ignores an uploaded certificate).
    signing: bool
    certificate: CertificateOut | None
    # osslsigncode is installed, so an uploaded certificate can be used.
    certificate_tool: bool
    # DATA_ENCRYPTION_KEY is set, so a certificate can be stored.
    certificate_storage: bool
    # The host that timestamps signatures (INSTALLER_TIMESTAMP_URL), if any.
    timestamp_host: str | None


class CertificateIn(BaseModel):
    pfx_base64: str = Field(min_length=1, max_length=400_000)
    password: str = Field(default="", max_length=256)


class ReleaseOut(BaseModel):
    tag: str
    version: str
    prerelease: bool
    published_at: str
    architectures: list[str]


class BuildIn(BaseModel):
    servers: ServersIn
    tag: str = Field(min_length=1, max_length=64)
    arch: Arch
    # Remove RustDesk's existing settings (and so its ID) before installing.
    reset_settings: bool = False
    # Sign with the uploaded certificate: omitted = if one can be used, true = it must, false = do not.
    sign: bool | None = None
    # Sets this as the permanent password once installed (rustdesk.exe --password). Never stored;
    # only reaches the generated script. Empty = leave the password alone.
    permanent_password: str = Field(default="", max_length=installer_service.MAX_PASSWORD_LENGTH)


class BuildOut(BaseModel):
    id: str
    state: str
    progress: int
    message: str
    tag: str
    version: str
    arch: str
    filename: str
    size: int
    signed: bool
    warnings: list[str]


class StoredFileOut(BaseModel):
    kind: str
    name: str
    size: int
    modified: datetime.datetime


class FilesOut(BaseModel):
    # Where they are on the server (INSTALLER_DIR, or next to the database).
    directory: str
    items: list[StoredFileOut]


class DeletedOut(BaseModel):
    deleted: int


def _servers(payload: ServersIn) -> client_config.ClientServers:
    return client_config.ClientServers(
        id_server=client_config.clean(payload.id_server),
        relay_server=client_config.clean(payload.relay_server),
        api_server=client_config.clean(payload.api_server).rstrip("/"),
        key=client_config.clean(payload.key, client_config.MAX_KEY),
    )


def _out(job: installer_service.Job) -> BuildOut:
    return BuildOut(
        id=job.id,
        state=job.state,
        progress=job.progress,
        message=job.message,
        tag=job.tag,
        version=job.version,
        arch=job.arch,
        filename=job.filename,
        size=job.size,
        signed=job.signed,
        warnings=job.warnings,
    )


def _raise(exc: installer_service.InstallerError) -> ApiError:
    return ApiError(exc.code, str(exc), exc.status)


@router.get("", response_model=StatusOut)
def status(
    _user: User = Depends(require_permission("settings", "view")),
    settings: Settings = Depends(get_settings_dep),
) -> StatusOut:
    certificate = signing_service.info(installer_service.installer_directory(settings))
    return StatusOut(
        available=installer_service.availability(settings) is None,
        reason=installer_service.availability(settings),
        signing=bool(settings.installer_sign_command),
        certificate=_certificate_out(certificate) if certificate else None,
        certificate_tool=signing_service.find_tool(settings) is not None,
        certificate_storage=bool(settings.data_encryption_key),
        timestamp_host=signing_service.timestamp_host(settings),
    )


def _certificate_out(certificate: signing_service.CertificateInfo) -> CertificateOut:
    return CertificateOut(
        subject=certificate.subject,
        issuer=certificate.issuer,
        thumbprint=certificate.thumbprint,
        not_before=certificate.not_before,
        not_after=certificate.not_after,
        expired=certificate.expired,
        code_signing=certificate.code_signing,
        uploaded_by=certificate.uploaded_by,
        uploaded_at=certificate.uploaded_at,
    )


@router.put(
    "/certificate",
    response_model=CertificateOut,
    dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)],
)
def upload_certificate(
    payload: CertificateIn,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
    settings: Settings = Depends(get_settings_dep),
) -> CertificateOut:
    """Stores the code-signing certificate (a .pfx / .p12 file, base64, with its password) that
    signs the installers built here. Write-only: it is kept encrypted with DATA_ENCRYPTION_KEY and
    is never returned. Uploading again replaces it."""
    try:
        pfx = base64.b64decode(payload.pfx_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError("BAD_REQUEST", "The certificate file was not sent correctly.", 422) from None
    if installer_service.is_building():
        raise _raise(installer_service.InstallerBusy())
    try:
        certificate = signing_service.store(
            settings, installer_service.installer_directory(settings), pfx, payload.password, admin.username
        )
    except signing_service.SigningError as exc:
        raise ApiError(exc.code, str(exc), exc.status) from None
    audit_service.record(
        db,
        action="installer_certificate_set",
        actor_id=admin.id,
        detail={
            "subject": certificate.subject,
            "thumbprint": certificate.thumbprint,
            "not_after": certificate.not_after.isoformat(),
        },
    )
    db.commit()
    return _certificate_out(certificate)


@router.delete("/certificate", response_model=DeletedOut, dependencies=[Depends(verify_csrf)])
def remove_certificate(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
    settings: Settings = Depends(get_settings_dep),
) -> DeletedOut:
    """Deletes the stored certificate and its private key."""
    if installer_service.is_building():
        raise _raise(installer_service.InstallerBusy())
    directory = installer_service.installer_directory(settings)
    previous = signing_service.info(directory)
    try:
        existed = signing_service.remove(directory)
    except signing_service.SigningError as exc:
        raise ApiError(exc.code, str(exc), exc.status) from None
    if existed:
        audit_service.record(
            db,
            action="installer_certificate_removed",
            actor_id=admin.id,
            detail={"thumbprint": previous.thumbprint if previous else None},
        )
        db.commit()
    return DeletedOut(deleted=1 if existed else 0)


@router.get("/releases", response_model=list[ReleaseOut])
def releases(_user: User = Depends(require_permission("settings", "view"))) -> list[ReleaseOut]:
    """The RustDesk releases that have a Windows MSI, newest first; the nightly build (a
    pre-release) is among them."""
    try:
        found = installer_service.list_releases()
    except installer_service.InstallerError as exc:
        raise _raise(exc) from exc
    return [
        ReleaseOut(
            tag=r.tag,
            version=r.version,
            prerelease=r.prerelease,
            published_at=r.published_at,
            architectures=sorted(r.assets),
        )
        for r in found
    ]


@router.post(
    "/builds",
    response_model=BuildOut,
    status_code=202,
    dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)],
)
def start_build(
    payload: BuildIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission("settings", "manage")),
    settings: Settings = Depends(get_settings_dep),
) -> BuildOut:
    """Starts building in the background; poll GET /builds/{id}. One build at a time."""
    try:
        job = installer_service.start_build(
            settings,
            installer_service.BuildSpec(
                servers=_servers(payload.servers),
                tag=payload.tag,
                arch=payload.arch,
                reset_settings=payload.reset_settings,
                sign_with_certificate=payload.sign,
                permanent_password=payload.permanent_password,
            ),
        )
    except installer_service.InstallerError as exc:
        raise _raise(exc) from exc
    audit_service.record(
        db,
        action="installer_build",
        actor_id=admin.id,
        detail={
            "tag": job.tag,
            "arch": job.arch,
            "reset_settings": payload.reset_settings,
            "signed": job.certificate or bool(settings.installer_sign_command),
            "certificate": job.certificate,
            "permanent_password": bool(payload.permanent_password),
        },
    )
    db.commit()
    return _out(job)


def _job(job_id: str) -> installer_service.Job:
    job = installer_service.get_job(job_id)
    if job is None:
        raise ApiError("BUILD_NOT_FOUND", "That build does not exist any more.", 404)
    return job


@router.get("/builds/{job_id}", response_model=BuildOut)
def build_status(job_id: str, _user: User = Depends(require_permission("settings", "view"))) -> BuildOut:
    return _out(_job(job_id))


@router.get("/builds/{job_id}/file")
def build_file(job_id: str, _user: User = Depends(require_permission("settings", "view"))) -> FileResponse:
    job = _job(job_id)
    if job.state != "done" or job.file is None or not job.file.is_file():
        raise ApiError("BUILD_NOT_READY", "The installer is not ready.", 409)
    return FileResponse(
        job.file,
        media_type="application/vnd.microsoft.portable-executable",
        filename=job.filename,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/kit", dependencies=[Depends(verify_csrf), Depends(enforce_auth_rate_limit)])
def download_kit(
    payload: BuildIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission("settings", "manage")),
) -> Response:
    """A zip with the NSIS script and a PowerShell script to build and sign the setup file
    on a machine of your choice."""
    try:
        release = installer_service.find_release(payload.tag)
        data = installer_service.build_kit(
            _servers(payload.servers),
            release,
            payload.arch,
            payload.reset_settings,
            payload.permanent_password,
        )
    except installer_service.InstallerError as exc:
        raise _raise(exc) from exc
    audit_service.record(
        db,
        action="installer_kit",
        actor_id=admin.id,
        detail={
            "tag": release.tag,
            "arch": payload.arch,
            "reset_settings": payload.reset_settings,
            "permanent_password": bool(payload.permanent_password),
        },
    )
    db.commit()
    filename = f"rustdesk-{release.version}-{payload.arch}-kit.zip"
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/files", response_model=FilesOut)
def stored_files(
    _user: User = Depends(require_permission("settings", "view")),
    settings: Settings = Depends(get_settings_dep),
) -> FilesOut:
    """What the installer builder keeps on the server: the RustDesk MSIs it downloaded and the
    setup files it built. Neither is needed to keep anything working; both are made again on demand."""
    return FilesOut(
        directory=str(installer_service.installer_directory(settings)),
        items=[
            StoredFileOut(kind=f.kind, name=f.name, size=f.size, modified=f.modified)
            for f in installer_service.list_files(settings)
        ],
    )


@router.get("/files/installer/{name}")
def download_stored_installer(
    name: str,
    _user: User = Depends(require_permission("settings", "view")),
    settings: Settings = Depends(get_settings_dep),
) -> FileResponse:
    """A setup file built earlier (it survives a restart of the server, the build jobs do not)."""
    try:
        path = installer_service.stored_file_path(settings, "installer", name)
    except installer_service.InstallerError as exc:
        raise _raise(exc) from exc
    return FileResponse(
        path,
        media_type="application/vnd.microsoft.portable-executable",
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/files/{kind}", response_model=DeletedOut, dependencies=[Depends(verify_csrf)])
def delete_all_files(
    kind: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission("settings", "manage")),
    settings: Settings = Depends(get_settings_dep),
) -> DeletedOut:
    """Deletes every stored file of one kind ('msi' or 'installer')."""
    return _delete(db, admin, settings, kind, None)


@router.delete("/files/{kind}/{name}", response_model=DeletedOut, dependencies=[Depends(verify_csrf)])
def delete_one_file(
    kind: str,
    name: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission("settings", "manage")),
    settings: Settings = Depends(get_settings_dep),
) -> DeletedOut:
    return _delete(db, admin, settings, kind, name)


def _delete(db: Session, admin: User, settings: Settings, kind: str, name: str | None) -> DeletedOut:
    try:
        count = installer_service.delete_files(settings, kind, name)
    except installer_service.InstallerError as exc:
        raise _raise(exc) from exc
    audit_service.record(
        db,
        action="installer_files_deleted",
        actor_id=admin.id,
        detail={"kind": kind, "name": name, "count": count},
    )
    db.commit()
    return DeletedOut(deleted=count)
