"""uc03_delivery_vehicle_photos.py — raw Delivery vehicle-photo upload,
deliberately outside DI (see vehicle_photo_storage.py's own docstring for
why). Plain evidence of the delivered vehicle, no classification, no
extraction, no requirement-catalog entry -- just a file and who uploaded it.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_delivery_capture_v2 import _authorize_delivery
from audit_core.vehicle_photo_storage import (
    VehiclePhotoStorage,
    VehiclePhotoStorageError,
    get_vehicle_photo_storage,
)

router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}/delivery/vehicle-photos",
    tags=["uc03-delivery-vehicle-photos"],
)

_MAX_PHOTOS_PER_JOURNEY = 12
_MAX_PHOTO_BYTES = 15 * 1024 * 1024
_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


def _sanitize_filename(filename: str) -> str:
    value = unicodedata.normalize("NFKD", filename or "photo")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return value[:80] or "photo"


class VehiclePhoto(BaseModel):
    photoId: UUID
    originalFilename: str
    contentType: str
    sizeBytes: int
    uploadedByActorId: str
    uploadedAtUtc: str
    contentUrl: str


class VehiclePhotoListResponse(BaseModel):
    photos: list[VehiclePhoto]


def _vehicle_photo_count(connection: Connection, *, tenant_id: str, journey_id: UUID) -> int:
    return connection.execute(
        text(
            """
            SELECT count(*) FROM auditcore.delivery_vehicle_photos
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND deleted_at_utc IS NULL
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()


def vehicle_photos_uploaded(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    """Used by the Delivery-completion gate (uc03_delivery_capture_v2) to
    decide whether to raise the vehicle-photos task -- kept here, next to
    the table it reads, rather than duplicating the query at the call site.
    """
    return _vehicle_photo_count(connection, tenant_id=tenant_id, journey_id=journey_id) > 0


def _public_photo(row, storage: VehiclePhotoStorage) -> VehiclePhoto:
    return VehiclePhoto(
        photoId=row["photo_id"],
        originalFilename=row["original_filename"],
        contentType=row["content_type"],
        sizeBytes=row["size_bytes"],
        uploadedByActorId=row["uploaded_by_actor_id"],
        uploadedAtUtc=row["uploaded_at_utc"].isoformat(),
        contentUrl=storage.get_presigned_url(row["object_key"]),
    )


@router.get("", response_model=VehiclePhotoListResponse)
def list_vehicle_photos(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    storage: Annotated[VehiclePhotoStorage, Depends(get_vehicle_photo_storage)],
) -> VehiclePhotoListResponse:
    _authorize_delivery(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    rows = connection.execute(
        text(
            """
            SELECT photo_id, original_filename, content_type, size_bytes, object_key,
                   uploaded_by_actor_id, uploaded_at_utc
            FROM auditcore.delivery_vehicle_photos
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND deleted_at_utc IS NULL
            ORDER BY uploaded_at_utc
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return VehiclePhotoListResponse(photos=[_public_photo(row, storage) for row in rows])


@router.post("", response_model=VehiclePhotoListResponse)
def upload_vehicle_photos(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    storage: Annotated[VehiclePhotoStorage, Depends(get_vehicle_photo_storage)],
    files: Annotated[list[UploadFile], File(...)],
) -> VehiclePhotoListResponse:
    _authorize_delivery(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    existing = _vehicle_photo_count(connection, tenant_id=tenant_id, journey_id=journey_id)
    if existing + len(files) > _MAX_PHOTOS_PER_JOURNEY:
        raise AuditCoreError(
            error_code="VAC-VAL-007",
            status_code=400,
            title="Too many vehicle photos",
            detail=f"At most {_MAX_PHOTOS_PER_JOURNEY} vehicle photos are kept per journey.",
        )

    stored_rows = []
    for upload in files:
        content_type = (upload.content_type or "").strip().lower()
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise AuditCoreError(
                error_code="VAC-VAL-008",
                status_code=400,
                title="Unsupported photo type",
                detail=f"{upload.filename}: only photo files (JPEG/PNG/WEBP/HEIC) are accepted.",
            )
        data = upload.file.read()
        if not data or len(data) > _MAX_PHOTO_BYTES:
            raise AuditCoreError(
                error_code="VAC-VAL-009",
                status_code=400,
                title="Invalid photo file",
                detail=f"{upload.filename}: file is empty or exceeds the size limit.",
            )
        photo_id = uuid4()
        object_key = (
            f"{tenant_id}/delivery-vehicle-photos/{journey_id}/"
            f"{photo_id}_{_sanitize_filename(upload.filename or 'photo.jpg')}"
        )
        try:
            storage.put_object(object_key, data, content_type)
        except VehiclePhotoStorageError as exc:
            raise AuditCoreError(
                error_code="VAC-DEP-001",
                status_code=502,
                title="Photo storage unavailable",
                detail="Could not store the uploaded photo. Please try again.",
            ) from exc
        row = connection.execute(
            text(
                """
                INSERT INTO auditcore.delivery_vehicle_photos (
                    tenant_id, photo_id, journey_id, object_key,
                    original_filename, content_type, size_bytes, uploaded_by_actor_id
                ) VALUES (
                    :tenant_id, :photo_id, :journey_id, :object_key,
                    :original_filename, :content_type, :size_bytes, :actor_id
                )
                RETURNING photo_id, original_filename, content_type, size_bytes, object_key,
                          uploaded_by_actor_id, uploaded_at_utc
                """
            ),
            {
                "tenant_id": tenant_id,
                "photo_id": photo_id,
                "journey_id": journey_id,
                "object_key": object_key,
                "original_filename": (upload.filename or "photo.jpg")[:255],
                "content_type": content_type,
                "size_bytes": len(data),
                "actor_id": human_principal.subject,
            },
        ).mappings().one()
        stored_rows.append(row)

    from audit_core.uc03_delivery_commands import _complete_open_vehicle_photos_task

    _complete_open_vehicle_photos_task(
        connection, tenant_id=tenant_id, journey_id=journey_id, actor_id=human_principal.subject,
    )
    return VehiclePhotoListResponse(photos=[_public_photo(row, storage) for row in stored_rows])


@router.delete("/{photo_id}", status_code=204)
def delete_vehicle_photo(
    tenant_id: str,
    journey_id: UUID,
    photo_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> None:
    _authorize_delivery(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    updated = connection.execute(
        text(
            """
            UPDATE auditcore.delivery_vehicle_photos
            SET deleted_at_utc = now(), deleted_by_actor_id = :actor_id
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND photo_id=:photo_id
              AND deleted_at_utc IS NULL
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "photo_id": photo_id,
            "actor_id": human_principal.subject,
        },
    ).rowcount
    if not updated:
        raise NotFoundError(
            error_code="VAC-NF-032",
            title="Vehicle photo not found",
            detail="This vehicle photo does not exist or was already removed.",
        )
