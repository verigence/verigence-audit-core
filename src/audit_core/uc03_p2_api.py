"""Isolated UC03 Phase 2 HTTP API.

All endpoints are additive under /p2/v1. Existing UC03 routes remain unchanged.
Security remains the sole authorization authority: Phase 2 performs one
Security permission decision and does not repeat the legacy DB-backed
business_assignments authorization path.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from pypdf import PdfReader, PdfWriter
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import DependencyUnavailableError, NotFoundError
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)
from audit_core.uc03_p2_stage import recompute_booking_stage
from audit_core.uc03_p2_tasks import create_p2_task, submit_action

router = APIRouter(
    prefix="/p2/v1/tenants/{tenant_id}",
    tags=["uc03-phase2"],
)

_READ_PERMISSION = "audit.journey.read"
_UPDATE_PERMISSION = "audit.journey.update"
_DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_DEFAULT_MAX_PDF_PAGES = 100


def _authorize(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID | None,
    human_principal: HumanPrincipal,
    authorization_client: SecurityAuthorizationClient,
    permission_key: str,
) -> str:
    try:
        decision = authorization_client.check_user_permission(
            user_id=human_principal.subject,
            tenant_id=tenant_id,
            permission_key=permission_key,
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="Phase 2 work is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )
    set_tenant_context(connection, tenant_id)
    if journey_id is not None:
        found = connection.execute(
            text(
                """
                SELECT 1 FROM auditcore.journeys
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one_or_none()
        if found is None:
            raise NotFoundError(
                error_code="VAC-NF-005",
                title="Journey not found",
                detail="Journey not found in the requested Tenant.",
            )
    return decision.role_key or "USER"


def _split_pages(payload: bytes, *, content_type: str | None, filename: str) -> list[bytes]:
    is_pdf = (content_type or "").lower() == "application/pdf" or filename.lower().endswith(".pdf")
    if not is_pdf:
        return [payload]

    try:
        reader = PdfReader(io.BytesIO(payload))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"{filename}: PDF could not be read.") from exc
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"{filename}: encrypted PDF is not supported.") from exc

    page_count = len(reader.pages)
    max_pages = int(os.environ.get("P2_MAX_PDF_PAGES", str(_DEFAULT_MAX_PDF_PAGES)))
    if page_count < 1:
        raise HTTPException(status_code=422, detail=f"{filename}: PDF contains no pages.")
    if page_count > max_pages:
        raise HTTPException(
            status_code=413,
            detail=f"{filename}: PDF contains {page_count} pages; maximum is {max_pages}.",
        )

    pages: list[bytes] = []
    for page in reader.pages:
        writer = PdfWriter()
        writer.add_page(page)
        output = io.BytesIO()
        writer.write(output)
        pages.append(output.getvalue())
    return pages


def _activity(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    event_type: str,
    subject_type: str | None,
    subject_id: str | None,
    details: dict[str, Any],
    correlation_id: str,
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


@router.post("/journeys/{journey_id}/uploads")
async def accept_uploads(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    files: Annotated[list[UploadFile], File(...)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    role = _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    if not files:
        raise HTTPException(status_code=422, detail="At least one file is required.")

    max_bytes = int(os.environ.get("P2_MAX_UPLOAD_BYTES", str(_DEFAULT_MAX_UPLOAD_BYTES)))
    correlation_id = get_correlation_id(request)
    accepted: list[dict[str, Any]] = []

    for upload in files:
        payload = await upload.read()
        filename = upload.filename or "document"
        if not payload:
            raise HTTPException(status_code=422, detail=f"{filename}: file is empty.")
        if len(payload) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{filename}: file exceeds the {max_bytes // (1024 * 1024)} MB limit.",
            )

        pages = _split_pages(payload, content_type=upload.content_type, filename=filename)
        batch_id = uuid4()
        digest = hashlib.sha256(payload).hexdigest()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_upload_batches (
                    tenant_id, batch_id, journey_id, original_filename,
                    content_type, size_bytes, sha256, page_count,
                    original_payload, batch_status, uploaded_by_actor_id,
                    correlation_id
                ) VALUES (
                    :tenant_id, :batch_id, :journey_id, :filename,
                    :content_type, :size_bytes, :sha256, :page_count,
                    :payload, 'ACCEPTED', :actor_id, :correlation_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "batch_id": batch_id,
                "journey_id": journey_id,
                "filename": filename,
                "content_type": upload.content_type,
                "size_bytes": len(payload),
                "sha256": digest,
                "page_count": len(pages),
                "payload": payload,
                "actor_id": human_principal.subject,
                "correlation_id": correlation_id,
            },
        )

        queued_pages: list[dict[str, Any]] = []
        for page_number, page_payload in enumerate(pages, start=1):
            queue_id = uuid4()
            page_sha = hashlib.sha256(page_payload).hexdigest()
            client_upload_id = f"p2-{batch_id}-{page_number}-{page_sha[:12]}"
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.p2_document_queue (
                        tenant_id, queue_id, batch_id, journey_id,
                        page_number, page_sha256, page_payload,
                        client_upload_id, queue_status, correlation_id
                    ) VALUES (
                        :tenant_id, :queue_id, :batch_id, :journey_id,
                        :page_number, :page_sha, :page_payload,
                        :client_upload_id, 'QUEUED', :correlation_id
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "queue_id": queue_id,
                    "batch_id": batch_id,
                    "journey_id": journey_id,
                    "page_number": page_number,
                    "page_sha": page_sha,
                    "page_payload": page_payload,
                    "client_upload_id": client_upload_id,
                    "correlation_id": correlation_id,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.p2_work_queue (
                        tenant_id, journey_id, work_type, work_key,
                        payload, work_status, correlation_id
                    ) VALUES (
                        :tenant_id, :journey_id, 'DOCUMENT_INGEST', :work_key,
                        CAST(:payload AS jsonb), 'PENDING', :correlation_id
                    )
                    ON CONFLICT (tenant_id, work_type, work_key) DO NOTHING
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "journey_id": journey_id,
                    "work_key": str(queue_id),
                    "payload": json.dumps({
                        "queueId": str(queue_id),
                        "batchId": str(batch_id),
                        "pageNumber": page_number,
                        "sourceFilename": filename,
                        "uploadedBy": human_principal.subject,
                        "uploadedByRole": role,
                    }),
                    "correlation_id": correlation_id,
                },
            )
            _activity(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                event_type="DOCUMENT_PAGE_QUEUED",
                subject_type="DOCUMENT_PAGE",
                subject_id=str(queue_id),
                details={"batchId": str(batch_id), "pageNumber": page_number},
                correlation_id=correlation_id,
            )
            queued_pages.append({
                "queueId": str(queue_id),
                "pageNumber": page_number,
                "status": "QUEUED",
            })

        _activity(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="UPLOAD_BATCH_ACCEPTED",
            subject_type="UPLOAD_BATCH",
            subject_id=str(batch_id),
            details={
                "filename": filename,
                "pageCount": len(pages),
                "sizeBytes": len(payload),
                "sha256": digest,
            },
            correlation_id=correlation_id,
        )
        accepted.append({
            "batchId": str(batch_id),
            "filename": filename,
            "pageCount": len(pages),
            "status": "ACCEPTED",
            "pages": queued_pages,
        })

    return {"journeyId": str(journey_id), "accepted": accepted}


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
    after: int = 0,
    limit: int = 100,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)] = None,  # type: ignore[assignment]
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ] = None,  # type: ignore[assignment]
    connection: Annotated[Connection, Depends(get_connection)] = None,  # type: ignore[assignment]
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
    booking = recompute_booking_stage(
        connection, tenant_id=tenant_id, journey_id=journey_id,
    )
    delivery = {
        "completionState": "IN_PROGRESS",
        "configuration": "PENDING_BUSINESS_RULES",
        "gates": [],
    }
    return {"journeyId": str(journey_id), "booking": booking, "delivery": delivery}


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
    booking = recompute_booking_stage(
        connection, tenant_id=tenant_id, journey_id=journey_id,
    )
    header = connection.execute(
        text(
            """
            SELECT j.journey_id, j.created_at_utc, j.updated_at_utc,
                   c.display_name AS customer_name,
                   d.dealer_name,
                   o.outlet_name,
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

    doc_stats = connection.execute(
        text(
            """
            SELECT
              COUNT(*) FILTER (WHERE association_status='ACTIVE') AS total_active,
              COUNT(*) FILTER (WHERE association_status='SUPERSEDED') AS superseded,
              COUNT(*) FILTER (WHERE association_status='ACTIVE' AND process_area='BOOKING') AS booking_docs,
              COUNT(*) FILTER (WHERE association_status='ACTIVE' AND process_area='DELIVERY') AS delivery_docs
            FROM auditcore.evidence
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    task_stats = connection.execute(
        text(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED')) AS open,
              COUNT(*) FILTER (WHERE task_status='VERIFIED_COMPLETE') AS completed,
              COUNT(*) FILTER (WHERE due_at_utc < now()
                AND task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED')) AS overdue
            FROM auditcore.p2_tasks
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    finding_stats = connection.execute(
        text(
            """
            SELECT
              COUNT(*) FILTER (WHERE finding_status IN ('OPEN','ACKNOWLEDGED')) AS open,
              COUNT(*) FILTER (WHERE finding_status='RESOLVED') AS resolved
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    payment_stats = connection.execute(
        text(
            """
            SELECT
              COALESCE(SUM(amount) FILTER (WHERE payment_stage='BOOKING'), 0) AS booking_total,
              COALESCE(SUM(amount) FILTER (WHERE payment_stage='DELIVERY'), 0) AS delivery_total,
              COUNT(*) FILTER (WHERE payment_stage='BOOKING') AS booking_receipts,
              COUNT(*) FILTER (WHERE payment_stage='DELIVERY') AS delivery_receipts
            FROM auditcore.payments
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    upload_stats = connection.execute(
        text(
            """
            SELECT
              COUNT(*) AS batches,
              COALESCE(SUM(page_count),0) AS pages,
              COUNT(*) FILTER (WHERE batch_status IN ('FAILED','PARTIAL_FAILURE')) AS failed_batches
            FROM auditcore.p2_upload_batches
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    return {
        "journey": {k: (str(v) if isinstance(v, UUID) else v) for k, v in dict(header).items()},
        "stage": booking,
        "documents": dict(doc_stats),
        "uploads": dict(upload_stats),
        "payments": {k: str(v) if hasattr(v, "as_tuple") else v for k, v in dict(payment_stats).items()},
        "tasks": dict(task_stats),
        "findings": dict(finding_stats),
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
    allowedActions: list[str] = Field(default_factory=lambda: [
        "REVIEW_DOCUMENT", "CORRECT_EXTRACTED_FIELD", "UPLOAD_DOCUMENT",
        "ADD_EVIDENCE", "ADD_COMMENT", "PROVIDE_FEEDBACK", "COMPLETE_ACTION",
    ])
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
    role = _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    task_id = uuid4()
    dedupe_key = f"human:{task_id}"
    created = create_p2_task(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        task_type=command.taskType,
        category=command.category,
        origin_kind="HUMAN",
        source_type="HUMAN_ACTION",
        source_code=None,
        dedupe_key=dedupe_key,
        title=command.title,
        description=command.description,
        reference=command.reference,
        severity=command.severity,
        priority=command.priority,
        assigned_role_code=command.assignedRoleCode,
        assigned_actor_id=command.assignedActorId,
        raised_by_actor_id=human_principal.subject,
        raised_by_role_code=role,
        allowed_actions=command.allowedActions,
        completion_protocol="REQUESTER_CONFIRMED",
        due_at_utc=command.dueAtUtc,
    )
    return {"taskId": str(created), "status": "READY"}


@router.get("/tasks")
def list_tasks(
    tenant_id: str,
    status: str | None = None,
    journey_id: UUID | None = None,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)] = None,  # type: ignore[assignment]
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ] = None,  # type: ignore[assignment]
    connection: Annotated[Connection, Depends(get_connection)] = None,  # type: ignore[assignment]
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
            SELECT task_id, journey_id, root_task_id, parent_task_id,
                   round_number, task_type, category, origin_kind,
                   source_type, source_code, title, description, reference,
                   severity, priority, assigned_role_code, assigned_actor_id,
                   raised_by_actor_id, raised_by_role_code, allowed_actions,
                   completion_protocol, task_status, due_at_utc,
                   created_at_utc, updated_at_utc
            FROM auditcore.p2_tasks
            WHERE tenant_id=:tenant_id
              AND (:journey_id IS NULL OR journey_id=:journey_id)
              AND (:status IS NULL OR task_status=:status)
            ORDER BY
              CASE priority WHEN 'URGENT' THEN 1 WHEN 'HIGH' THEN 2
                            WHEN 'NORMAL' THEN 3 ELSE 4 END,
              due_at_utc NULLS LAST,
              created_at_utc
            LIMIT 500
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "status": status},
    ).mappings().all()
    items = []
    for row in rows:
        item = dict(row)
        for key in ("task_id", "journey_id", "root_task_id", "parent_task_id"):
            if item.get(key) is not None:
                item[key] = str(item[key])
        items.append(item)
    return {"items": items}


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
    role = _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=None,
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
            actor_role_code=role,
            comment=command.comment,
            details=command.details,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
