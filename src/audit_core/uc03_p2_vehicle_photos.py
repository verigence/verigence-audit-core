"""Phase 2 vehicle photos: a separate capture path from documents.

A vehicle photo is plain evidence of the physical vehicle. It is never sent
to DI, never classified and never extracted, so it has none of the document
pipeline's latency or failure modes: the browser uploads straight to object
storage with a presigned URL and one small finalize call records it.

    1. :upload-intents   deterministic object key per clientUploadId + URL
    2. PUT to storage    browser -> object storage, no Audit Core bandwidth
    3. :finalize         HEAD the object (no transaction open), then one
                         short transaction inserts the row idempotently

Photos are available at any stage (PCs often photograph the car at booking
too). A PC may also add PDFs here (product owner 2026-10-30): at most
``MAX_PDFS_PER_JOURNEY`` per booking, each up to ``MAX_PDF_BYTES``; they are
stored as they are, like the photos, and count among them. Delivery readiness counts them, and a machine-verified task asks for
them once Delivery is under way and none are on file.
"""
from __future__ import annotations

import hashlib
import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from audit_core import uc03_p2_storage
from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import (
    authorize_p2,
    check_p2_permission,
    resolve_p2_scope,
)
from audit_core.uc03_p2_runtime import note_facts_changed, record_activity

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-vehicle-photos"])

_READ_PERMISSION = "audit.journey.read"
_UPDATE_PERMISSION = "audit.journey.update"
MAX_PHOTOS_PER_JOURNEY = 24
MAX_PHOTO_BYTES = 15 * 1024 * 1024
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PDFS_PER_JOURNEY = 3
PDF_TYPE = "application/pdf"
_CONTENT_TYPES = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic", "image/heif": "heif",
    PDF_TYPE: "pdf",
}
VIEW_CODES = ("FRONT", "REAR", "LEFT", "RIGHT", "INTERIOR", "ODOMETER", "CHASSIS", "DELIVERY", "OTHER")
_CLIENT_UPLOAD_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class PhotoFile(BaseModel):
    clientUploadId: str = Field(min_length=8, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    contentType: str
    sizeBytes: int = Field(gt=0)
    viewCode: str | None = None


class PhotoIntents(BaseModel):
    files: list[PhotoFile] = Field(min_length=1, max_length=MAX_PHOTOS_PER_JOURNEY)


class PhotoFinalize(BaseModel):
    clientUploadId: str = Field(min_length=8, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    contentType: str
    viewCode: str | None = None


def object_key_for(tenant_id: str, journey_id: UUID, client_upload_id: str, content_type: str) -> str:
    digest = hashlib.sha256(client_upload_id.encode("utf-8")).hexdigest()[:40]
    return f"{tenant_id}/p2-vehicle-photos/{journey_id}/{digest}.{_CONTENT_TYPES[content_type]}"


def _validate(file: PhotoFile | PhotoFinalize) -> tuple[str, str | None]:
    if not _CLIENT_UPLOAD_ID.match(file.clientUploadId):
        raise HTTPException(status_code=422, detail="clientUploadId has invalid characters.")
    content_type = file.contentType.strip().lower()
    if content_type not in _CONTENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"{file.filename}: only photos (JPEG, PNG, WEBP or HEIC) or PDF files can be added here.",
        )
    view = (file.viewCode or "").strip().upper() or None
    if view is not None and view not in VIEW_CODES:
        raise HTTPException(status_code=422, detail=f"Unknown photo view {file.viewCode}.")
    return content_type, view


def _active_count(connection: Connection, *, tenant_id: str, journey_id: UUID) -> int:
    return int(
        connection.execute(
            text(
                """
                SELECT COUNT(*) FROM auditcore.delivery_vehicle_photos
                WHERE tenant_id=:t AND journey_id=:j AND deleted_at_utc IS NULL
                """
            ),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()
    )


def _active_pdf_count(connection: Connection, *, tenant_id: str, journey_id: UUID) -> int:
    return int(
        connection.execute(
            text(
                """
                SELECT COUNT(*) FROM auditcore.delivery_vehicle_photos
                WHERE tenant_id=:t AND journey_id=:j AND deleted_at_utc IS NULL AND content_type=:pdf
                """
            ),
            {"t": tenant_id, "j": journey_id, "pdf": PDF_TYPE},
        ).scalar_one()
    )


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def size_error(filename: str, content_type: str, size: int) -> str | None:
    """Why a file of this size cannot be added, or None."""
    if size <= 0:
        return f"{filename} is empty."
    if content_type == PDF_TYPE and size > MAX_PDF_BYTES:
        return (f"{filename} is {_megabytes(size)}. A PDF can be at most {MAX_PDF_BYTES // (1024 * 1024)} MB: "
                "split it or reduce its size and upload again.")
    if content_type != PDF_TYPE and size > MAX_PHOTO_BYTES:
        return f"{filename} is {_megabytes(size)}. A photo can be at most {MAX_PHOTO_BYTES // (1024 * 1024)} MB."
    return None


def pdf_limit_error(already: int) -> str:
    return (f"At most {MAX_PDFS_PER_JOURNEY} PDFs can be added per booking ({already} already added). "
            "Remove one to add another.")


def _existing(connection: Connection, *, tenant_id: str, journey_id: UUID, client_upload_id: str):
    return connection.execute(
        text(
            """
            SELECT photo_id, original_filename, content_type, size_bytes, object_key, view_code,
                   uploaded_by_actor_id, uploaded_at_utc, deleted_at_utc
            FROM auditcore.delivery_vehicle_photos
            WHERE tenant_id=:t AND journey_id=:j AND client_upload_id=:c
            """
        ),
        {"t": tenant_id, "j": journey_id, "c": client_upload_id},
    ).mappings().one_or_none()


def _public(row: Any, *, url: str | None) -> dict[str, Any]:
    return {
        "photoId": str(row["photo_id"]),
        "filename": row["original_filename"],
        "contentType": row["content_type"],
        "sizeBytes": int(row["size_bytes"]),
        "viewCode": row["view_code"],
        "uploadedByActorId": row["uploaded_by_actor_id"],
        "uploadedAtUtc": row["uploaded_at_utc"].isoformat() if row["uploaded_at_utc"] else None,
        "url": url,
    }


def _signed_url(object_key: str) -> str | None:
    try:
        return uc03_p2_storage.get_p2_document_storage().presign_get(object_key)
    except (RuntimeError, uc03_p2_storage.P2DocumentStorageError):
        return None


@router.get("/journeys/{journey_id}/vehicle-photos")
def list_vehicle_photos(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    authorize_p2(
        connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_READ_PERMISSION,
    )
    rows = connection.execute(
        text(
            """
            SELECT photo_id, original_filename, content_type, size_bytes, object_key, view_code,
                   uploaded_by_actor_id, uploaded_at_utc
            FROM auditcore.delivery_vehicle_photos
            WHERE tenant_id=:t AND journey_id=:j AND deleted_at_utc IS NULL
            ORDER BY uploaded_at_utc, photo_id
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    return {
        "photos": [_public(r, url=_signed_url(str(r["object_key"]))) for r in rows],
        "limit": MAX_PHOTOS_PER_JOURNEY,
        "maxBytes": MAX_PHOTO_BYTES,
        "pdfLimit": MAX_PDFS_PER_JOURNEY,
        "maxPdfBytes": MAX_PDF_BYTES,
        "views": list(VIEW_CODES),
    }


@router.post("/journeys/{journey_id}/vehicle-photos:upload-intents")
def create_vehicle_photo_intents(
    tenant_id: str,
    journey_id: UUID,
    command: PhotoIntents,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> dict[str, Any]:
    decision = check_p2_permission(
        tenant_id=tenant_id, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_UPDATE_PERMISSION,
    )
    for file in command.files:
        content_type, _ = _validate(file)
        problem = size_error(file.filename, content_type, file.sizeBytes)
        if problem:
            raise HTTPException(status_code=422, detail=problem)
    with engine.begin() as connection:
        resolve_p2_scope(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            human_principal=human_principal, decision=decision,
        )
        active = _active_count(connection, tenant_id=tenant_id, journey_id=journey_id)
        active_pdfs = _active_pdf_count(connection, tenant_id=tenant_id, journey_id=journey_id)
        stored = {
            f.clientUploadId: _existing(connection, tenant_id=tenant_id, journey_id=journey_id,
                                        client_upload_id=f.clientUploadId)
            for f in command.files
        }
    new = [f for f in command.files if stored.get(f.clientUploadId) is None]
    if active + len(new) > MAX_PHOTOS_PER_JOURNEY:
        raise HTTPException(
            status_code=422,
            detail=f"At most {MAX_PHOTOS_PER_JOURNEY} vehicle photos are kept per booking "
                   f"({active} already on file).",
        )
    if active_pdfs + sum(1 for f in new if f.contentType.strip().lower() == PDF_TYPE) > MAX_PDFS_PER_JOURNEY:
        raise HTTPException(status_code=422, detail=pdf_limit_error(active_pdfs))
    try:
        storage = uc03_p2_storage.get_p2_document_storage()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Photo storage is not configured.") from exc
    uploads = []
    for file in command.files:
        existing = stored.get(file.clientUploadId)
        if existing is not None and existing["deleted_at_utc"] is None:
            uploads.append({"clientUploadId": file.clientUploadId, "photoId": str(existing["photo_id"]),
                            "alreadyStored": True})
            continue
        content_type = file.contentType.strip().lower()
        key = object_key_for(tenant_id, journey_id, file.clientUploadId, content_type)
        try:
            url = storage.presign_put(key, content_type=content_type)
        except uc03_p2_storage.P2DocumentStorageError as exc:
            raise HTTPException(status_code=503, detail="Photo storage is unavailable. Try again.") from exc
        uploads.append({
            "clientUploadId": file.clientUploadId,
            "uploadUrl": url,
            "uploadHeaders": {"Content-Type": content_type},
            "expiresInSeconds": 900,
            "alreadyStored": False,
        })
    return {"uploads": uploads}


@router.post("/journeys/{journey_id}/vehicle-photos:finalize")
def finalize_vehicle_photo(
    tenant_id: str,
    journey_id: UUID,
    command: PhotoFinalize,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> dict[str, Any]:
    content_type, view = _validate(command)
    decision = check_p2_permission(
        tenant_id=tenant_id, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_UPDATE_PERMISSION,
    )
    with engine.begin() as connection:
        resolve_p2_scope(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            human_principal=human_principal, decision=decision,
        )
        existing = _existing(connection, tenant_id=tenant_id, journey_id=journey_id,
                             client_upload_id=command.clientUploadId)
    if existing is not None and existing["deleted_at_utc"] is None:
        return _public(existing, url=_signed_url(str(existing["object_key"])))
    if existing is not None:
        raise HTTPException(status_code=409, detail="This photo was removed. Upload it again as a new photo.")

    key = object_key_for(tenant_id, journey_id, command.clientUploadId, content_type)
    try:
        metadata = uc03_p2_storage.get_p2_document_storage().head_object(key)
    except (RuntimeError, uc03_p2_storage.P2DocumentStorageError) as exc:
        raise HTTPException(status_code=409, detail="The photo has not finished uploading. Retry.") from exc
    size = int(metadata["contentLength"])
    problem = size_error(command.filename, content_type, size)
    if problem:
        raise HTTPException(status_code=422, detail=problem)

    correlation_id = get_correlation_id(request)
    with engine.begin() as connection:
        resolve_p2_scope(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            human_principal=human_principal, decision=decision,
        )
        # Serialise photo writes for this Journey so the limit holds under
        # concurrent finalize calls.
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"p2-vehicle-photos:{tenant_id}:{journey_id}"},
        )
        if _active_count(connection, tenant_id=tenant_id, journey_id=journey_id) >= MAX_PHOTOS_PER_JOURNEY:
            raise HTTPException(
                status_code=422, detail=f"At most {MAX_PHOTOS_PER_JOURNEY} vehicle photos are kept per booking.",
            )
        if content_type == PDF_TYPE:
            pdfs = _active_pdf_count(connection, tenant_id=tenant_id, journey_id=journey_id)
            if pdfs >= MAX_PDFS_PER_JOURNEY:
                raise HTTPException(status_code=422, detail=pdf_limit_error(pdfs))
        row = connection.execute(
            text(
                """
                INSERT INTO auditcore.delivery_vehicle_photos (
                    tenant_id, journey_id, object_key, original_filename, content_type, size_bytes,
                    uploaded_by_actor_id, client_upload_id, view_code, capture_source
                ) VALUES (:t, :j, :key, :name, :type, :size, :actor, :client, :view, 'P2')
                ON CONFLICT (tenant_id, journey_id, client_upload_id) WHERE client_upload_id IS NOT NULL
                DO NOTHING
                RETURNING photo_id, original_filename, content_type, size_bytes, object_key, view_code,
                          uploaded_by_actor_id, uploaded_at_utc
                """
            ),
            {
                "t": tenant_id, "j": journey_id, "key": key, "name": command.filename[:255],
                "type": content_type, "size": size, "actor": human_principal.subject,
                "client": command.clientUploadId, "view": view,
            },
        ).mappings().one_or_none()
        if row is None:
            row = _existing(connection, tenant_id=tenant_id, journey_id=journey_id,
                            client_upload_id=command.clientUploadId)
        else:
            record_activity(
                connection, tenant_id=tenant_id, journey_id=journey_id, event_type="VEHICLE_PHOTO_ADDED",
                subject_type="VEHICLE_PHOTO", subject_id=str(row["photo_id"]),
                details={"filename": row["original_filename"], "viewCode": view}, correlation_id=correlation_id,
            )
            note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                               reason="VEHICLE_PHOTO_ADDED", correlation_id=correlation_id)
    return _public(row, url=_signed_url(key))


@router.delete("/journeys/{journey_id}/vehicle-photos/{photo_id}", status_code=204)
def delete_vehicle_photo(
    tenant_id: str,
    journey_id: UUID,
    photo_id: UUID,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> None:
    authorize_p2(
        connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_UPDATE_PERMISSION,
    )
    removed = connection.execute(
        text(
            """
            UPDATE auditcore.delivery_vehicle_photos
            SET deleted_at_utc=now(), deleted_by_actor_id=:actor
            WHERE tenant_id=:t AND journey_id=:j AND photo_id=:p AND deleted_at_utc IS NULL
            """
        ),
        {"t": tenant_id, "j": journey_id, "p": photo_id, "actor": human_principal.subject},
    ).rowcount
    if not removed:
        raise HTTPException(status_code=404, detail="Photo was not found.")
    correlation_id = get_correlation_id(request)
    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="VEHICLE_PHOTO_REMOVED",
        subject_type="VEHICLE_PHOTO", subject_id=str(photo_id), details={}, correlation_id=correlation_id,
    )
    note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                       reason="VEHICLE_PHOTO_REMOVED", correlation_id=correlation_id)
