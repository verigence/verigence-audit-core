"""Isolated UC03 Phase 2 HTTP API.

Everything here is additive under /p2/v1. Existing UC03 APIs stay unchanged.

Key runtime rules:
- browser uploads directly to existing S3-compatible Audit Core storage
- Audit Core only acknowledges after the object exists and a durable work row is committed
- PDF splitting, DI submission, reconciliation, rule/task verification are worker work
- P2 routes use one access adapter: Security permission + existing business-assignment Journey scope
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import DependencyUnavailableError
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import P2AccessContext, authorize_p2
from audit_core.uc03_p2_stage import read_booking_stage
from audit_core.uc03_p2_storage import (
    P2DocumentStorageError,
    get_p2_document_storage,
)
from audit_core.uc03_p2_tasks import create_p2_task, submit_action

router = APIRouter(
    prefix="/p2/v1/tenants/{tenant_id}",
    tags=["uc03-phase2"],
)

_READ_PERMISSION = "audit.journey.read"
_UPDATE_PERMISSION = "audit.journey.update"
_DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
}


def _authorize(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID | None,
    human_principal: HumanPrincipal,
    authorization_client: SecurityAuthorizationClient,
    permission_key: str,
) -> P2AccessContext:
    return authorize_p2(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=permission_key,
    )


def _safe_filename(value: str) -> str:
    name = value.strip() or "document"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name[:180] or "document"


def _activity(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    event_type: str,
    subject_type: str | None,
    subject_id: str | None,
    details: dict[str, Any],
    correlation_id: str | None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_activity_events (
                tenant_id, journey_id, event_type, subject_type,
                subject_id, details, correlation_id
            ) VALUES (
                :tenant_id, :journey_id, :event_type, :subject_type,
                :subject_id, CAST(:details AS jsonb), :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "event_type": event_type,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "details": json.dumps(details, default=str),
            "correlation_id": correlation_id,
        },
    )


@router.get("/journeys")
def list_p2_journeys(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    q: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=None,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    search = (q or "").strip()
    rows = connection.execute(
        text(
            """
            WITH scoped AS (
                SELECT j.tenant_id, j.journey_id, j.customer_id, j.dealer_id,
                       j.outlet_id, j.created_at_utc, j.updated_at_utc
                FROM auditcore.journeys j
                WHERE j.tenant_id=:tenant_id
                  AND EXISTS (
                    SELECT 1
                    FROM auditcore.business_assignments ba
                    WHERE ba.tenant_id=j.tenant_id
                      AND ba.security_actor_id=:actor_id
                      AND ba.assignment_status='ACTIVE'
                      AND ba.effective_from <= now()
                      AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                      AND (
                        ba.dealer_id IS NULL
                        OR (
                          ba.dealer_id=j.dealer_id
                          AND (ba.outlet_id IS NULL OR ba.outlet_id=j.outlet_id)
                        )
                      )
                  )
            ),
            task_rows AS (
                SELECT tenant_id, journey_id, due_at_utc,
                       task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED') AS is_open
                FROM auditcore.p2_tasks
                WHERE tenant_id=:tenant_id
                UNION ALL
                SELECT tenant_id, subject_ref AS journey_id, due_at_utc,
                       status IN ('OPEN','IN_PROGRESS') AS is_open
                FROM auditcore.work_items
                WHERE tenant_id=:tenant_id
                  AND item_kind='EXECUTION_TASK'
                  AND subject_kind='JOURNEY'
            ),
            task_stats AS (
                SELECT tenant_id, journey_id,
                       COUNT(*) AS total_tasks,
                       COUNT(*) FILTER (WHERE is_open) AS open_tasks,
                       COUNT(*) FILTER (
                         WHERE is_open AND due_at_utc < now()
                       ) AS overdue_tasks
                FROM task_rows
                GROUP BY tenant_id, journey_id
            ),
            finding_stats AS (
                SELECT tenant_id, journey_id,
                       COUNT(*) FILTER (
                         WHERE finding_status IN ('OPEN','ACKNOWLEDGED')
                       ) AS open_findings
                FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id
                GROUP BY tenant_id, journey_id
            ),
            document_stats AS (
                SELECT tenant_id, journey_id,
                       COUNT(*) FILTER (WHERE association_status='ACTIVE') AS documents
                FROM auditcore.evidence
                WHERE tenant_id=:tenant_id
                GROUP BY tenant_id, journey_id
            )
            SELECT s.journey_id,
                   c.display_name AS customer_name,
                   c.mobile_last4,
                   d.dealer_name,
                   o.outlet_name,
                   NULLIF(concat_ws(' · ',
                     NULLIF(jp.model_name_snapshot,''),
                     NULLIF(jp.variant_name_snapshot,''),
                     NULLIF(jp.colour_name_snapshot,'')
                   ), '') AS vehicle,
                   COALESCE(pr.current_stage, 'BOOKING_DOCUMENT_UPLOAD') AS current_stage,
                   COALESCE(pr.booking_completion_state, 'IN_PROGRESS') AS booking_completion_state,
                   COALESCE(pr.delivery_completion_state, 'IN_PROGRESS') AS delivery_completion_state,
                   COALESCE(pr.booking_receipt_total,0) AS booking_receipt_total,
                   pr.booking_minimum_amount,
                   COALESCE(pr.manual_verification_pending_count,0) AS manual_verification_pending_count,
                   COALESCE(ds.documents,0) AS documents,
                   COALESCE(ts.total_tasks,0) AS total_tasks,
                   COALESCE(ts.open_tasks,0) AS open_tasks,
                   COALESCE(ts.overdue_tasks,0) AS overdue_tasks,
                   COALESCE(fs.open_findings,0) AS open_findings,
                   s.updated_at_utc
            FROM scoped s
            JOIN auditcore.customers c
              ON c.tenant_id=s.tenant_id AND c.customer_id=s.customer_id
            JOIN auditcore.dealers d
              ON d.tenant_id=s.tenant_id AND d.dealer_id=s.dealer_id
            JOIN auditcore.dealer_outlets o
              ON o.tenant_id=s.tenant_id AND o.dealer_id=s.dealer_id
             AND o.outlet_id=s.outlet_id
            LEFT JOIN auditcore.journey_products jp
              ON jp.tenant_id=s.tenant_id AND jp.journey_id=s.journey_id
            LEFT JOIN auditcore.p2_journey_runtime pr
              ON pr.tenant_id=s.tenant_id AND pr.journey_id=s.journey_id
            LEFT JOIN document_stats ds
              ON ds.tenant_id=s.tenant_id AND ds.journey_id=s.journey_id
            LEFT JOIN task_stats ts
              ON ts.tenant_id=s.tenant_id AND ts.journey_id=s.journey_id
            LEFT JOIN finding_stats fs
              ON fs.tenant_id=s.tenant_id AND fs.journey_id=s.journey_id
            WHERE (
              :search = ''
              OR c.display_name ILIKE '%' || :search || '%'
              OR d.dealer_name ILIKE '%' || :search || '%'
              OR o.outlet_name ILIKE '%' || :search || '%'
              OR COALESCE(jp.model_name_snapshot,'') ILIKE '%' || :search || '%'
              OR s.journey_id::text ILIKE '%' || :search || '%'
            )
            ORDER BY
              COALESCE(ts.overdue_tasks,0) DESC,
              COALESCE(ts.open_tasks,0) DESC,
              s.updated_at_utc DESC
            LIMIT :limit
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": human_principal.subject,
            "search": search,
            "limit": min(max(limit, 1), 250),
        },
    ).mappings().all()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["journey_id"] = str(item["journey_id"])
        for key in ("booking_receipt_total", "booking_minimum_amount"):
            if item.get(key) is not None:
                item[key] = str(item[key])
        items.append(item)
    return {"items": items}


class UploadInitFile(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    contentType: str = Field(min_length=1, max_length=160)
    sizeBytes: int = Field(gt=0)
    clientUploadId: str | None = Field(default=None, max_length=160)


class UploadInitCommand(BaseModel):
    files: list[UploadInitFile] = Field(min_length=1, max_length=50)


@router.post("/journeys/{journey_id}/uploads:init")
def init_uploads(
    tenant_id: str,
    journey_id: UUID,
    command: UploadInitCommand,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    max_bytes = int(os.environ.get("P2_MAX_UPLOAD_BYTES", str(_DEFAULT_MAX_UPLOAD_BYTES)))
    correlation_id = get_correlation_id(request)
    try:
        storage = get_p2_document_storage()
    except RuntimeError as exc:
        raise DependencyUnavailableError(
            detail="Phase 2 document storage is not configured."
        ) from exc

    prepared: list[dict[str, Any]] = []
    for item in command.files:
        content_type = item.contentType.lower().strip()
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=415,
                detail=f"{item.filename}: unsupported content type {item.contentType}.",
            )
        if item.sizeBytes > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{item.filename}: file exceeds the {max_bytes // (1024 * 1024)} MB limit.",
            )
        batch_id = uuid4()
        safe_name = _safe_filename(item.filename)
        object_key = (
            f"p2-documents/{tenant_id}/{journey_id}/{batch_id}/original/{safe_name}"
        )
        try:
            upload_url = storage.presign_put(
                object_key,
                content_type=content_type,
                expires_seconds=900,
            )
        except P2DocumentStorageError as exc:
            raise DependencyUnavailableError(
                detail="The document upload could not be prepared."
            ) from exc

        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_upload_batches (
                    tenant_id, batch_id, journey_id, original_filename,
                    content_type, size_bytes, page_count,
                    original_object_key, batch_status, uploaded_by_actor_id,
                    correlation_id
                ) VALUES (
                    :tenant_id, :batch_id, :journey_id, :filename,
                    :content_type, :size_bytes, 0,
                    :object_key, 'AWAITING_UPLOAD', :actor_id,
                    :correlation_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "batch_id": batch_id,
                "journey_id": journey_id,
                "filename": item.filename,
                "content_type": content_type,
                "size_bytes": item.sizeBytes,
                "object_key": object_key,
                "actor_id": human_principal.subject,
                "correlation_id": correlation_id,
            },
        )
        _activity(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="UPLOAD_INITIALIZED",
            subject_type="UPLOAD_BATCH",
            subject_id=str(batch_id),
            details={
                "filename": item.filename,
                "contentType": content_type,
                "sizeBytes": item.sizeBytes,
                "clientUploadId": item.clientUploadId,
            },
            correlation_id=correlation_id,
        )
        prepared.append(
            {
                "batchId": str(batch_id),
                "clientUploadId": item.clientUploadId,
                "filename": item.filename,
                "uploadUrl": upload_url,
                "uploadHeaders": {"Content-Type": content_type},
                "expiresInSeconds": 900,
            }
        )

    return {"journeyId": str(journey_id), "uploads": prepared}


@router.post("/journeys/{journey_id}/uploads/{batch_id}:finalize")
def finalize_upload(
    tenant_id: str,
    journey_id: UUID,
    batch_id: UUID,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    access = _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    batch = connection.execute(
        text(
            """
            SELECT batch_id, original_filename, content_type, size_bytes,
                   original_object_key, batch_status, uploaded_by_actor_id
            FROM auditcore.p2_upload_batches
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND batch_id=:batch_id
            FOR UPDATE
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "batch_id": batch_id,
        },
    ).mappings().one_or_none()
    if batch is None:
        raise HTTPException(status_code=404, detail="Upload batch was not found.")
    if batch["batch_status"] in {"UPLOADED", "SPLITTING", "PROCESSING", "COMPLETED"}:
        return {"batchId": str(batch_id), "status": str(batch["batch_status"])}

    try:
        metadata = get_p2_document_storage().head_object(str(batch["original_object_key"]))
    except (RuntimeError, P2DocumentStorageError) as exc:
        raise HTTPException(
            status_code=409,
            detail="The uploaded object is not available yet. Retry finalize.",
        ) from exc

    expected_size = int(batch["size_bytes"])
    actual_size = int(metadata["contentLength"])
    if expected_size != actual_size:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Uploaded file size does not match the prepared upload "
                f"({actual_size} vs {expected_size} bytes)."
            ),
        )

    correlation_id = get_correlation_id(request)
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_upload_batches
            SET batch_status='UPLOADED', updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND batch_id=:batch_id
            """
        ),
        {"tenant_id": tenant_id, "batch_id": batch_id},
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_work_queue (
                tenant_id, journey_id, work_type, work_key,
                payload, work_status, correlation_id
            ) VALUES (
                :tenant_id, :journey_id, 'SPLIT_BATCH', :work_key,
                CAST(:payload AS jsonb), 'PENDING', :correlation_id
            )
            ON CONFLICT (tenant_id, work_type, work_key)
            DO UPDATE SET work_status='PENDING',
                          next_attempt_at_utc=NULL,
                          last_error=NULL,
                          updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "work_key": str(batch_id),
            "payload": json.dumps(
                {
                    "batchId": str(batch_id),
                    "uploadedBy": str(batch["uploaded_by_actor_id"]),
                    "uploadedByRole": access.operating_role or access.functional_role or "USER",
                }
            ),
            "correlation_id": correlation_id,
        },
    )
    _activity(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        event_type="UPLOAD_ACCEPTED",
        subject_type="UPLOAD_BATCH",
        subject_id=str(batch_id),
        details={
            "filename": str(batch["original_filename"]),
            "sizeBytes": actual_size,
        },
        correlation_id=correlation_id,
    )
    return {"batchId": str(batch_id), "status": "UPLOADED"}


@router.get("/journeys/{journey_id}/documents")
def list_documents(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    batches = connection.execute(
        text(
            """
            SELECT batch_id, original_filename, content_type, size_bytes,
                   sha256, page_count, batch_status, created_at_utc, updated_at_utc
            FROM auditcore.p2_upload_batches
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            ORDER BY created_at_utc DESC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    pages = connection.execute(
        text(
            """
            SELECT queue_id, batch_id, page_number, client_upload_id,
                   di_document_id, classified_document_type, business_stage,
                   queue_status, attempt_count, extracted_field_count,
                   last_error, created_at_utc, updated_at_utc
            FROM auditcore.p2_document_queue
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            ORDER BY created_at_utc DESC, page_number
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    by_batch: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        item = dict(page)
        batch_key = str(item.pop("batch_id"))
        item["queueId"] = str(item.pop("queue_id"))
        if item.get("di_document_id") is not None:
            item["diDocumentId"] = str(item.pop("di_document_id"))
        by_batch.setdefault(batch_key, []).append(item)

    result = []
    for batch in batches:
        item = dict(batch)
        batch_key = str(item.pop("batch_id"))
        item["batchId"] = batch_key
        item["pages"] = by_batch.get(batch_key, [])
        result.append(item)
    return {"journeyId": str(journey_id), "batches": result}


@router.get("/journeys/{journey_id}/events")
def list_events(
    tenant_id: str,
    journey_id: UUID,
    after: int,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    limit: int = 100,
) -> dict[str, Any]:
    _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    rows = connection.execute(
        text(
            """
            SELECT event_id, event_type, subject_type, subject_id,
                   details, created_at_utc
            FROM auditcore.p2_activity_events
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND event_id > :after
            ORDER BY event_id
            LIMIT :limit
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "after": max(0, after),
            "limit": min(max(limit, 1), 250),
        },
    ).mappings().all()
    return {"events": [dict(row) for row in rows]}


@router.get("/journeys/{journey_id}/stage")
def stage_status(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    booking = read_booking_stage(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    return {
        "journeyId": str(journey_id),
        "booking": booking,
        "delivery": {
            "completionState": "IN_PROGRESS",
            "configuration": "PENDING_BUSINESS_RULES",
            "gates": [],
        },
    }


@router.get("/journeys/{journey_id}/overview")
def overview_summary(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    booking = read_booking_stage(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    header = connection.execute(
        text(
            """
            SELECT j.journey_id, j.created_at_utc, j.updated_at_utc,
                   c.display_name AS customer_name,
                   d.dealer_name, o.outlet_name,
                   NULLIF(concat_ws(' · ',
                       NULLIF(jp.model_name_snapshot, ''),
                       NULLIF(jp.variant_name_snapshot, ''),
                       NULLIF(jp.colour_name_snapshot, '')
                   ), '') AS vehicle
            FROM auditcore.journeys j
            JOIN auditcore.customers c
              ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            JOIN auditcore.dealers d
              ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
            JOIN auditcore.dealer_outlets o
              ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id
             AND o.outlet_id=j.outlet_id
            LEFT JOIN auditcore.journey_products jp
              ON jp.tenant_id=j.tenant_id AND jp.journey_id=j.journey_id
            WHERE j.tenant_id=:tenant_id AND j.journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    document_stats = connection.execute(
        text(
            """
            SELECT
              COUNT(*) FILTER (WHERE association_status='ACTIVE') AS total_active,
              COUNT(*) FILTER (WHERE association_status='SUPERSEDED') AS superseded,
              COUNT(*) FILTER (
                WHERE association_status='ACTIVE' AND process_area='BOOKING'
              ) AS booking_docs,
              COUNT(*) FILTER (
                WHERE association_status='ACTIVE' AND process_area='DELIVERY'
              ) AS delivery_docs
            FROM auditcore.evidence
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    p2_upload = connection.execute(
        text(
            """
            SELECT COUNT(*) AS batches,
                   COALESCE(SUM(page_count),0) AS pages,
                   COUNT(*) FILTER (
                     WHERE batch_status IN ('FAILED','PARTIAL_FAILURE')
                   ) AS failed_batches
            FROM auditcore.p2_upload_batches
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    tasks = connection.execute(
        text(
            """
            WITH task_rows AS (
                SELECT due_at_utc,
                       task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED') AS is_open,
                       task_status='VERIFIED_COMPLETE' AS is_completed
                FROM auditcore.p2_tasks
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                UNION ALL
                SELECT due_at_utc,
                       status IN ('OPEN','IN_PROGRESS') AS is_open,
                       status='RESOLVED' AS is_completed
                FROM auditcore.work_items
                WHERE tenant_id=:tenant_id
                  AND subject_kind='JOURNEY'
                  AND subject_ref=:journey_id
                  AND item_kind='EXECUTION_TASK'
            )
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE is_open) AS open,
                   COUNT(*) FILTER (WHERE is_completed) AS completed,
                   COUNT(*) FILTER (WHERE is_open AND due_at_utc < now()) AS overdue
            FROM task_rows
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    findings = connection.execute(
        text(
            """
            SELECT COUNT(*) FILTER (
                     WHERE finding_status IN ('OPEN','ACKNOWLEDGED')
                   ) AS open,
                   COUNT(*) FILTER (
                     WHERE finding_status='RESOLVED'
                   ) AS resolved
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    payments = connection.execute(
        text(
            """
            SELECT COALESCE(SUM(amount) FILTER (
                     WHERE payment_stage='BOOKING'
                   ),0) AS booking_total,
                   COALESCE(SUM(amount) FILTER (
                     WHERE payment_stage='DELIVERY'
                   ),0) AS delivery_total,
                   COUNT(*) FILTER (
                     WHERE payment_stage='BOOKING'
                   ) AS booking_receipts,
                   COUNT(*) FILTER (
                     WHERE payment_stage='DELIVERY'
                   ) AS delivery_receipts
            FROM auditcore.payments
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    def serializable(row: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in dict(row).items():
            if isinstance(value, UUID) or hasattr(value, "as_tuple"):
                result[key] = str(value)
            else:
                result[key] = value
        return result

    return {
        "journey": serializable(header),
        "stage": booking,
        "documents": serializable(document_stats),
        "uploads": serializable(p2_upload),
        "payments": serializable(payments),
        "tasks": serializable(tasks),
        "findings": serializable(findings),
    }


class HumanTaskCreate(BaseModel):
    taskType: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    severity: str = "MEDIUM"
    priority: str = "NORMAL"
    assignedRoleCode: str = Field(min_length=1, max_length=80)
    assignedActorId: str | None = None
    dueAtUtc: datetime | None = None
    allowedActions: list[str] = Field(
        default_factory=lambda: [
            "REVIEW_DOCUMENT",
            "CORRECT_EXTRACTED_FIELD",
            "UPLOAD_DOCUMENT",
            "REUPLOAD_DOCUMENT",
            "ADD_EVIDENCE",
            "ADD_COMMENT",
            "PROVIDE_FEEDBACK",
            "COMPLETE_ACTION",
        ]
    )
    reference: dict[str, Any] = Field(default_factory=dict)


@router.post("/journeys/{journey_id}/tasks")
def create_human_task(
    tenant_id: str,
    journey_id: UUID,
    command: HumanTaskCreate,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    access = _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    unique = uuid4()
    created = create_p2_task(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        task_type=command.taskType,
        category=command.category,
        origin_kind="HUMAN",
        source_type="HUMAN_ACTION",
        source_code=None,
        dedupe_key=f"human:{unique}",
        title=command.title,
        description=command.description,
        reference=command.reference,
        severity=command.severity,
        priority=command.priority,
        assigned_role_code=command.assignedRoleCode,
        assigned_actor_id=command.assignedActorId,
        raised_by_actor_id=human_principal.subject,
        raised_by_role_code=access.operating_role or access.functional_role or "USER",
        allowed_actions=command.allowedActions,
        completion_protocol="REQUESTER_CONFIRMED",
        due_at_utc=command.dueAtUtc,
    )
    return {"taskId": str(created), "status": "READY"}


@router.get("/tasks")
def list_tasks(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    status: str | None = None,
    journey_id: UUID | None = None,
) -> dict[str, Any]:
    _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )

    # P2-native tasks own the new completion protocols. Existing workflow
    # tasks are NOT copied into p2_tasks: migrations 0097/0099 already mirror
    # every legacy workflow task into work_items/work_item_task_detail,
    # including historical backfill. Reading that mirror avoids duplicate
    # tasks and preserves the old task engine's task-specific completion hooks.
    p2_rows = connection.execute(
        text(
            """
            SELECT task_id, journey_id, root_task_id, parent_task_id,
                   round_number, task_type, category, origin_kind,
                   source_type, source_code, title, description, reference,
                   severity, priority, assigned_role_code, assigned_actor_id,
                   raised_by_actor_id, raised_by_role_code, allowed_actions,
                   completion_protocol, task_status, due_at_utc,
                   created_at_utc, updated_at_utc
            FROM auditcore.p2_tasks t
            WHERE t.tenant_id=:tenant_id
              AND (:journey_id IS NULL OR t.journey_id=:journey_id)
              AND (
                (:status IS NULL AND t.task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED'))
                OR (:status IS NOT NULL AND t.task_status=:status)
              )
              AND EXISTS (
                SELECT 1
                FROM auditcore.journeys j
                JOIN auditcore.business_assignments ba
                  ON ba.tenant_id=j.tenant_id
                 AND ba.security_actor_id=:actor_id
                 AND ba.assignment_status='ACTIVE'
                 AND ba.effective_from <= now()
                 AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                 AND (
                   ba.dealer_id IS NULL
                   OR (
                     ba.dealer_id=j.dealer_id
                     AND (ba.outlet_id IS NULL OR ba.outlet_id=j.outlet_id)
                   )
                 )
                WHERE j.tenant_id=t.tenant_id AND j.journey_id=t.journey_id
              )
            ORDER BY t.due_at_utc NULLS LAST, t.created_at_utc
            LIMIT 500
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "status": status,
            "actor_id": human_principal.subject,
        },
    ).mappings().all()

    legacy_rows = connection.execute(
        text(
            """
            SELECT
              wi.work_item_id AS task_id,
              wi.subject_ref AS journey_id,
              wtd.task_type,
              wi.origin_kind,
              wi.owner_role_code AS assigned_role_code,
              wi.assigned_actor_id,
              wi.priority AS priority_rank,
              wi.due_at_utc,
              wi.status AS task_status,
              wi.title,
              COALESCE(
                NULLIF(wtd.task_payload->>'comment',''),
                NULLIF(wtd.task_payload->>'description',''),
                NULLIF(wtd.last_error_summary,''),
                wi.summary
              ) AS description,
              COALESCE(wtd.severity, 'MEDIUM') AS severity,
              wtd.process_area,
              wtd.effect_key,
              wtd.related_finding_id,
              wtd.task_payload AS reference,
              wi.created_at_utc,
              wi.updated_at_utc
            FROM auditcore.work_items wi
            JOIN auditcore.work_item_task_detail wtd
              ON wtd.tenant_id=wi.tenant_id
             AND wtd.work_item_id=wi.work_item_id
            JOIN auditcore.journeys j
              ON j.tenant_id=wi.tenant_id
             AND j.journey_id=wi.subject_ref
            WHERE wi.tenant_id=:tenant_id
              AND wi.item_kind='EXECUTION_TASK'
              AND wi.subject_kind='JOURNEY'
              AND (:journey_id IS NULL OR wi.subject_ref=:journey_id)
              AND (
                (:status IS NULL AND wi.status IN ('OPEN','IN_PROGRESS'))
                OR (:status IS NOT NULL AND wi.status=:status)
              )
              AND EXISTS (
                SELECT 1
                FROM auditcore.business_assignments ba
                WHERE ba.tenant_id=j.tenant_id
                  AND ba.security_actor_id=:actor_id
                  AND ba.assignment_status='ACTIVE'
                  AND ba.effective_from <= now()
                  AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                  AND (
                    ba.dealer_id IS NULL
                    OR (
                      ba.dealer_id=j.dealer_id
                      AND (ba.outlet_id IS NULL OR ba.outlet_id=j.outlet_id)
                    )
                  )
              )
            ORDER BY wi.due_at_utc NULLS LAST, wi.priority DESC, wi.created_at_utc
            LIMIT 500
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "status": status,
            "actor_id": human_principal.subject,
        },
    ).mappings().all()

    items: list[dict[str, Any]] = []
    for row in p2_rows:
        item = dict(row)
        for key in ("task_id", "journey_id", "root_task_id", "parent_task_id"):
            if item.get(key) is not None:
                item[key] = str(item[key])
        item["source_system"] = "P2"
        item["priority_rank"] = None
        item["legacy_queue_url"] = None
        items.append(item)

    for row in legacy_rows:
        raw = dict(row)
        reference = dict(raw.get("reference") or {})
        task_id = str(raw["task_id"])
        journey = str(raw["journey_id"])
        source_code = (
            reference.get("ruleKey")
            or reference.get("ruleCode")
            or raw.get("effect_key")
        )
        items.append(
            {
                "task_id": task_id,
                "journey_id": journey,
                "root_task_id": None,
                "parent_task_id": None,
                "round_number": 1,
                "task_type": str(raw["task_type"]),
                "category": str(raw["task_type"]),
                "origin_kind": str(raw["origin_kind"] or "SYSTEM"),
                "source_type": "LEGACY_WORKFLOW_TASK",
                "source_code": str(source_code) if source_code else None,
                "title": str(raw["title"] or raw["task_type"]),
                "description": str(
                    raw["description"]
                    or "Existing Verigence workflow task. Open the existing Task Queue for its current action flow."
                ),
                "reference": reference,
                "severity": str(raw["severity"] or "MEDIUM"),
                # Preserve the legacy queue's native numeric priority rather
                # than inventing a label translation that could change meaning.
                "priority": None,
                "priority_rank": int(raw["priority_rank"] or 0),
                "assigned_role_code": raw["assigned_role_code"],
                "assigned_actor_id": raw["assigned_actor_id"],
                "raised_by_actor_id": None,
                "raised_by_role_code": None,
                "allowed_actions": [],
                "completion_protocol": "LEGACY_WORKFLOW",
                "task_status": str(raw["task_status"]),
                "due_at_utc": raw["due_at_utc"],
                "created_at_utc": raw["created_at_utc"],
                "updated_at_utc": raw["updated_at_utc"],
                "source_system": "LEGACY",
                "legacy_queue_url": "/reviews",
                "process_area": raw["process_area"],
                "related_finding_id": (
                    str(raw["related_finding_id"])
                    if raw["related_finding_id"] is not None
                    else None
                ),
            }
        )

    # Python cannot compare timezone-aware and naive datetime.max directly.
    # Convert the sort key to timestamp text, which is stable ISO order for
    # PostgreSQL timestamptz values and keeps NULL due dates last.
    def _safe_sort(item: dict[str, Any]) -> tuple:
        due = item.get("due_at_utc")
        created = item.get("created_at_utc")
        return (
            due is None,
            due.isoformat() if hasattr(due, "isoformat") else str(due or ""),
            created.isoformat() if hasattr(created, "isoformat") else str(created or ""),
        )

    items.sort(key=_safe_sort)
    return {
        "items": items[:500],
        "sources": {
            "p2": len(p2_rows),
            "legacy": len(legacy_rows),
        },
    }


class TaskActionCommand(BaseModel):
    action: str
    comment: str | None = Field(default=None, max_length=4000)
    details: dict[str, Any] = Field(default_factory=dict)


@router.post("/tasks/{task_id}/actions")
def task_action(
    tenant_id: str,
    task_id: UUID,
    command: TaskActionCommand,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=None,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    task_journey_id = connection.execute(
        text(
            """
            SELECT journey_id
            FROM auditcore.p2_tasks
            WHERE tenant_id=:tenant_id AND task_id=:task_id
            """
        ),
        {"tenant_id": tenant_id, "task_id": task_id},
    ).scalar_one_or_none()
    if task_journey_id is None:
        raise HTTPException(status_code=404, detail="Task was not found.")
    access = _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=UUID(str(task_journey_id)),
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    try:
        return submit_action(
            connection,
            tenant_id=tenant_id,
            task_id=task_id,
            action=command.action,
            actor_id=human_principal.subject,
            actor_role_code=access.operating_role or access.functional_role or "USER",
            comment=command.comment,
            details=command.details,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
