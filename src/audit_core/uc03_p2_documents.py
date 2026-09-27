"""Phase 2 document review/edit endpoints.

The UI is new, but the evidence contracts are not duplicated:
- DI remains the source of document bytes, page/box coordinates and machine facts.
- Audit Core durable reviewed fields remain the source of corrections/effective values.
- The existing typed correction materializer is reused so P2 cannot diverge from
  current business-field persistence.
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import DependencyUnavailableError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient
from audit_core.uc03_document_capture_v2 import (
    _DI_AUDIENCE,
    _ensure_di_context,
    get_di_client,
    get_security_oauth_client,
)
from audit_core.uc03_document_field_corrections import _apply_field_value
from audit_core.uc03_p2_access import (
    authorize_p2,
    check_p2_permission,
    resolve_p2_scope,
)
from audit_core.uc03_p2_runtime import note_facts_changed, record_activity
from audit_core.uc03_p2_tasks import create_p2_task
from audit_core.uc03_review_confidence import requires_pc_review

router = APIRouter(
    prefix="/p2/v1/tenants/{tenant_id}/journeys/{journey_id}",
    tags=["uc03-phase2-documents"],
)

_READ_PERMISSION = "audit.journey.read"
_UPDATE_PERMISSION = "audit.journey.update"


def _document_context(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT
                d.di_document_id,
                d.stage_code,
                d.classified_document_type_key,
                d.original_filename,
                e.evidence_id,
                e.document_type_key AS evidence_document_type,
                q.queue_id,
                q.queue_status,
                q.classified_document_type AS p2_document_type,
                q.business_stage AS p2_stage,
                b.original_filename AS p2_original_filename
            FROM auditcore.document_capture_v2_documents d
            LEFT JOIN auditcore.evidence e
              ON e.tenant_id=d.tenant_id
             AND e.journey_id=d.journey_id
             AND e.di_document_id=d.di_document_id
             AND e.association_status='ACTIVE'
            LEFT JOIN auditcore.p2_document_queue q
              ON q.tenant_id=d.tenant_id
             AND q.journey_id=d.journey_id
             AND q.di_document_id=d.di_document_id
            LEFT JOIN auditcore.p2_upload_batches b
              ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
            WHERE d.tenant_id=:tenant_id
              AND d.journey_id=:journey_id
              AND d.di_document_id=:document_id
              AND d.capture_status <> 'SUPERSEDED'
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
        },
    ).mappings().one_or_none()
    if row is None:
        legacy = connection.execute(
            text(
                """
                SELECT di_document_id, evidence_id, document_type_key,
                       process_area
                FROM auditcore.evidence
                WHERE tenant_id=:tenant_id
                  AND journey_id=:journey_id
                  AND di_document_id=:document_id
                  AND association_status='ACTIVE'
                LIMIT 1
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "document_id": document_id,
            },
        ).mappings().one_or_none()
        if legacy is None:
            raise HTTPException(status_code=404, detail="Document was not found.")
        return {
            "di_document_id": legacy["di_document_id"],
            "stage_code": str(legacy["process_area"] or "BOOKING"),
            "document_type_key": legacy["document_type_key"],
            "original_filename": "Document",
            "evidence_id": legacy["evidence_id"],
            "queue_status": None,
        }

    return {
        "di_document_id": row["di_document_id"],
        "stage_code": str(row["p2_stage"] or row["stage_code"] or "BOOKING"),
        "document_type_key": (
            row["p2_document_type"]
            or row["classified_document_type_key"]
            or row["evidence_document_type"]
        ),
        "original_filename": (
            row["p2_original_filename"]
            or row["original_filename"]
            or "Document"
        ),
        "evidence_id": row["evidence_id"],
        "queue_id": row["queue_id"],
        "queue_status": row["queue_status"],
    }


def _durable_fields(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT source_canonical_field_id, field_key, source_fact_version,
                   extracted_value, modified_value, effective_value,
                   confidence_score, confidence_scale, is_modified,
                   reviewed_by_actor_id, reviewed_at_utc, stage_code,
                   source_document_type_key
            FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND di_document_id=:document_id
            ORDER BY field_key, source_fact_version DESC
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _related_tasks(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT task_id, task_type, title, task_status,
                   source_type, source_code, created_at_utc
            FROM auditcore.p2_tasks
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND (
                reference->>'documentId'=:document_id
                OR reference->>'document_id'=:document_id
              )
            ORDER BY created_at_utc DESC
            LIMIT 50
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": str(document_id),
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _durable_lookup(rows: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        canonical = str(row.get("source_canonical_field_id") or "")
        version = int(row.get("source_fact_version") or 0)
        if canonical and version > 0:
            result[(canonical, version)] = row
    return result


@router.get("/documents/{document_id}")
@router.get("/documents/{document_id}/review")
def get_p2_document_review(
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[
        SecurityOAuthClient, Depends(get_security_oauth_client)
    ],
    di_client: Annotated[DiClient, Depends(get_di_client)],
) -> dict[str, Any]:
    decision = check_p2_permission(
        tenant_id=tenant_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    security_client.get_service_token(audience=_DI_AUDIENCE)
    with engine.begin() as connection:
        resolve_p2_scope(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            human_principal=human_principal,
            decision=decision,
        )
        context = _document_context(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document_id,
        )
        durable = _durable_fields(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document_id,
        )
        related_task_rows = _related_tasks(
            connection, tenant_id=tenant_id, journey_id=journey_id, document_id=document_id,
        )
        context_ref, token = _ensure_di_context(
            connection=connection,
            engine=engine,
            tenant_id=tenant_id,
            journey_id=journey_id,
            security_client=security_client,
            di_client=di_client,
        )
    lookup = _durable_lookup(durable)

    # DI reads happen with no database transaction open.
    di_document = None
    di_facts = ()
    di_error: str | None = None
    try:
        di_document = di_client.get_audit_document(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            document_id=str(document_id),
        )
        di_facts = di_client.get_audit_document_facts(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            document_id=str(document_id),
        )
    except DiClientError as exc:
        di_error = exc.code
        if not durable:
            raise DependencyUnavailableError(
                detail="Document facts are temporarily unavailable."
            ) from exc

    fields: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for fact in di_facts:
        key = (fact.canonical_field_id, int(fact.version_no))
        persisted = lookup.get(key)
        seen.add(key)
        fields.append(
            {
                "canonicalFieldId": fact.canonical_field_id,
                "fieldKey": fact.field_key,
                "sourceFactVersion": int(fact.version_no),
                "extractedValue": fact.value,
                "modifiedValue": persisted.get("modified_value") if persisted else None,
                "effectiveValue": (
                    persisted.get("effective_value")
                    if persisted and persisted.get("effective_value") is not None
                    else fact.value
                ),
                "confidenceScore": fact.confidence_score,
                "isModified": bool(persisted.get("is_modified")) if persisted else False,
                "reviewedByActorId": persisted.get("reviewed_by_actor_id") if persisted else None,
                "reviewedAtUtc": persisted.get("reviewed_at_utc") if persisted else None,
                "pageNo": fact.page_no,
                "evidenceRegion": fact.evidence_region,
            }
        )

    # Fail-soft DI read: durable facts remain reviewable even when DI's field
    # endpoint is temporarily unavailable. Box coordinates/content will return
    # once DI recovers; values are not fabricated.
    for row in durable:
        canonical = str(row.get("source_canonical_field_id") or "")
        version = int(row.get("source_fact_version") or 0)
        if canonical and (canonical, version) in seen:
            continue
        fields.append(
            {
                "canonicalFieldId": canonical or str(row["field_key"]),
                "fieldKey": str(row["field_key"]),
                "sourceFactVersion": version,
                "extractedValue": row.get("extracted_value"),
                "modifiedValue": row.get("modified_value"),
                "effectiveValue": row.get("effective_value"),
                "confidenceScore": (
                    float(row["confidence_score"])
                    if row.get("confidence_score") is not None
                    else None
                ),
                "isModified": bool(row.get("is_modified")),
                "reviewedByActorId": row.get("reviewed_by_actor_id"),
                "reviewedAtUtc": row.get("reviewed_at_utc"),
                "pageNo": None,
                "evidenceRegion": None,
            }
        )

    correction_history = [
        {
            "fieldKey": str(row["field_key"]),
            "canonicalFieldId": (
                str(row["source_canonical_field_id"])
                if row.get("source_canonical_field_id")
                else None
            ),
            "sourceFactVersion": int(row.get("source_fact_version") or 0),
            "extractedValue": row.get("extracted_value"),
            "effectiveValue": row.get("effective_value"),
            "reviewedByActorId": row.get("reviewed_by_actor_id"),
            "reviewedAtUtc": row.get("reviewed_at_utc"),
        }
        for row in durable
        if bool(row.get("is_modified"))
    ]
    related_tasks = [
        {
            "taskId": str(row["task_id"]),
            "taskType": str(row["task_type"]),
            "title": str(row["title"]),
            "status": str(row["task_status"]),
            "sourceType": str(row["source_type"]),
            "sourceCode": (
                str(row["source_code"]) if row["source_code"] is not None else None
            ),
            "createdAtUtc": row["created_at_utc"],
        }
        for row in related_task_rows
    ]
    related_rules = sorted(
        {
            str(row["source_code"])
            for row in related_task_rows
            if row["source_type"] == "RULE" and row["source_code"]
        }
    )

    return {
        "journeyId": str(journey_id),
        "documentId": str(document_id),
        "documentTypeKey": context["document_type_key"],
        "stage": context["stage_code"],
        "originalFilename": context["original_filename"],
        "processingStatus": (
            di_document.processing_status
            if di_document is not None
            else context.get("queue_status")
        ),
        "confirmationStatus": (
            di_document.confirmation_status if di_document is not None else None
        ),
        "contentAvailable": di_document is not None,
        "diReadError": di_error,
        "fields": fields,
        "correctionHistory": correction_history,
        "relatedTasks": related_tasks,
        "relatedRules": related_rules,
    }



@router.delete("/documents/{document_id}", status_code=204)
def delete_p2_document(
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> Response:
    authorize_p2(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    context = _document_context(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        document_id=document_id,
    )
    stage_code = str(context["stage_code"] or "BOOKING").upper()
    connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET association_status='VOIDED',
                void_reason='P2_DOCUMENT_VOIDED',
                voided_by_actor_id=:actor_id,
                voided_at_utc=now()
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND di_document_id=:document_id
              AND association_status='ACTIVE'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
            "actor_id": human_principal.subject,
        },
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_documents
            SET capture_status='SUPERSEDED', updated_at_utc=now()
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND di_document_id=:document_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
        },
    )
    queue_id = context.get("queue_id")
    if queue_id is not None:
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_document_queue
                SET queue_status='CANCELLED',
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND queue_id=:queue_id
                """
            ),
            {"tenant_id": tenant_id, "queue_id": queue_id},
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_work_queue
                SET work_status='CANCELLED',
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id
                  AND journey_id=:journey_id
                  AND work_key=:work_key
                  AND work_type IN ('DOCUMENT_INGEST','DOCUMENT_RECONCILE')
                  AND work_status NOT IN ('COMPLETED','DEAD_LETTER','CANCELLED')
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "work_key": str(queue_id),
            },
        )

    # Recompute canonical projections from the remaining ACTIVE evidence.
    # This is deliberately fail-closed: if projection recomputation fails,
    # the request transaction rolls back the evidence void as well.
    if stage_code == "DELIVERY":
        from audit_core.uc03_delivery_post_extraction_materialization import (
            materialize_delivery_documents_from_durable_store,
        )

        materialize_delivery_documents_from_durable_store(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
    else:
        from audit_core.uc03_delivery_post_extraction_materialization import (
            materialize_booking_documents_from_durable_store,
        )

        materialize_booking_documents_from_durable_store(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )

    note_facts_changed(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        reason="DOCUMENT_VOIDED",
    )
    record_activity(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        event_type="DOCUMENT_VOIDED",
        subject_type="DOCUMENT",
        subject_id=str(document_id),
        details={"stage": stage_code},
    )
    return Response(status_code=204)


@router.get("/documents/{document_id}/content")
def get_p2_document_content(
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[
        SecurityOAuthClient, Depends(get_security_oauth_client)
    ],
    di_client: Annotated[DiClient, Depends(get_di_client)],
) -> Response:
    decision = check_p2_permission(
        tenant_id=tenant_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    security_client.get_service_token(audience=_DI_AUDIENCE)
    with engine.begin() as connection:
        resolve_p2_scope(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            human_principal=human_principal,
            decision=decision,
        )
        _document_context(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document_id,
        )
        context_ref, token = _ensure_di_context(
            connection=connection,
            engine=engine,
            tenant_id=tenant_id,
            journey_id=journey_id,
            security_client=security_client,
            di_client=di_client,
        )
    try:
        payload, content_type, content_disposition = (
            di_client.get_audit_document_content(
                token=token,
                tenant_id=tenant_id,
                external_context_ref=context_ref,
                document_id=str(document_id),
            )
        )
    except DiClientError as exc:
        raise DependencyUnavailableError(
            detail="Source document content is temporarily unavailable."
        ) from exc
    headers = {"Cache-Control": "private, no-store"}
    if content_disposition:
        headers["Content-Disposition"] = content_disposition
    return Response(content=payload, media_type=content_type, headers=headers)


class P2FieldCorrectionCommand(BaseModel):
    canonicalFieldId: str = Field(min_length=1, max_length=160)
    fieldKey: str = Field(min_length=1, max_length=160)
    sourceFactVersion: int = Field(gt=0)
    newValue: Any = Field(...)
    remarks: str | None = Field(default=None, max_length=2000)



class P2FieldConfirmCommand(BaseModel):
    canonicalFieldId: str = Field(min_length=1, max_length=160)
    sourceFactVersion: int = Field(gt=0)


@router.post("/documents/{document_id}/fields/{field_key}:confirm")
def confirm_p2_document_field(
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    field_key: str,
    command: P2FieldConfirmCommand,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The PC checked the page and the extracted value is right: record the
    review without changing the value (the machine value stays effective)."""
    authorize_p2(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    _document_context(connection, tenant_id=tenant_id, journey_id=journey_id, document_id=document_id)
    confirmed = connection.execute(
        text(
            """
            UPDATE auditcore.journey_document_extracted_fields
            SET reviewed_by_actor_id=:actor_id, reviewed_at_utc=now(), updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND di_document_id=:document_id
              AND field_key=:field_key AND source_canonical_field_id=:canonical_field_id
              AND source_fact_version=:source_fact_version
            RETURNING field_key
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
            "field_key": field_key,
            "canonical_field_id": command.canonicalFieldId,
            "source_fact_version": command.sourceFactVersion,
            "actor_id": human_principal.subject,
        },
    ).scalars().all()
    if not confirmed:
        raise HTTPException(
            status_code=409,
            detail="The extracted field changed. Refresh the document and retry.",
        )
    note_facts_changed(
        connection, tenant_id=tenant_id, journey_id=journey_id, reason="FIELD_CONFIRMED",
    )
    return {"documentId": str(document_id), "fieldKey": field_key, "confirmed": True}


@router.patch("/documents/{document_id}/fields/{field_key}")
def patch_p2_document_field(
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    field_key: str,
    command: P2FieldCorrectionCommand,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    if command.fieldKey != field_key:
        raise HTTPException(
            status_code=422,
            detail="Field key in the path and correction payload must match.",
        )
    return correct_p2_document_field(
        tenant_id=tenant_id,
        journey_id=journey_id,
        document_id=document_id,
        command=command,
        human_principal=human_principal,
        authorization_client=authorization_client,
        connection=connection,
    )


@router.post("/documents/{document_id}/field-corrections")
def correct_p2_document_field(
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    command: P2FieldCorrectionCommand,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    access = authorize_p2(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    context = _document_context(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        document_id=document_id,
    )
    field = connection.execute(
        text(
            """
            SELECT evidence_id, source_document_type_key, field_key,
                   source_canonical_field_id, source_fact_version,
                   confidence_score, extracted_value, effective_value,
                   is_modified
            FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND di_document_id=:document_id
              AND source_canonical_field_id=:canonical_field_id
              AND source_fact_version=:source_fact_version
              AND field_key=:field_key
            ORDER BY updated_at_utc DESC
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
            "canonical_field_id": command.canonicalFieldId,
            "source_fact_version": command.sourceFactVersion,
            "field_key": command.fieldKey,
        },
    ).mappings().one_or_none()
    if field is None:
        raise HTTPException(
            status_code=409,
            detail="The extracted field changed. Refresh the document and retry.",
        )

    confidence = (
        float(field["confidence_score"])
        if field["confidence_score"] is not None
        else None
    )
    # The machine (DI) value is immutable evidence: every correction writes a
    # new modified/effective value and carries the original extraction
    # forward unchanged, however many times the field is corrected.
    machine_value = field["extracted_value"]
    current_effective = (
        field["effective_value"]
        if field["effective_value"] is not None
        else machine_value
    )

    if requires_pc_review(confidence):
        _apply_field_value(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=str(context["stage_code"]),
            document_id=document_id,
            evidence_id=field["evidence_id"],
            document_type_key=(
                field["source_document_type_key"]
                or context["document_type_key"]
            ),
            field_key=command.fieldKey,
            canonical_field_id=command.canonicalFieldId,
            source_fact_version=command.sourceFactVersion,
            confidence_score=confidence,
            original_value=machine_value,
            new_value=command.newValue,
            actor_id=human_principal.subject,
        )
        note_facts_changed(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            reason="FIELD_CORRECTED",
        )
        return {
            "documentId": str(document_id),
            "fieldKey": command.fieldKey,
            "applied": True,
            "taskId": None,
        }

    if not (command.remarks or "").strip():
        raise HTTPException(
            status_code=422,
            detail=(
                "Remarks are required when correcting a field at or above "
                "90% confidence."
            ),
        )

    proposed_json = json.dumps(command.newValue, default=str, sort_keys=True)
    proposal_hash = hashlib.sha256(proposed_json.encode("utf-8")).hexdigest()[:16]
    reference = {
        "kind": "FIELD_CORRECTION",
        "documentId": str(document_id),
        "documentTypeKey": (
            field["source_document_type_key"] or context["document_type_key"]
        ),
        "stage": str(context["stage_code"]),
        "evidenceId": (
            str(field["evidence_id"]) if field["evidence_id"] is not None else None
        ),
        "canonicalFieldId": command.canonicalFieldId,
        "fieldKey": command.fieldKey,
        "sourceFactVersion": command.sourceFactVersion,
        "confidenceScore": confidence,
        "originalValue": machine_value,
        "currentEffectiveValue": current_effective,
        "proposedValue": command.newValue,
        "proposalRemarks": command.remarks.strip(),
    }
    task_id = create_p2_task(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        task_type="FIELD_CORRECTION_REVIEW_P2",
        category="CORRECTION_APPROVAL",
        origin_kind="SYSTEM",
        source_type="DOCUMENT_FIELD",
        source_code="FIELD_CORRECTION_REVIEW_P2",
        dedupe_key=(
            f"field-correction:{journey_id}:{document_id}:"
            f"{command.canonicalFieldId}:{command.sourceFactVersion}:{proposal_hash}"
        ),
        title=f"Review correction · {command.fieldKey.replace('_', ' ')}",
        description=(
            f"{command.fieldKey.replace('_', ' ')} on "
            f"{context['original_filename']} was extracted at "
            f"{confidence:.1f}% confidence. Review the proposed correction "
            "and approve or reject it."
        ),
        reference=reference,
        severity="MEDIUM",
        priority="NORMAL",
        assigned_role_code="TL",
        assigned_actor_id=None,
        raised_by_actor_id=human_principal.subject,
        raised_by_role_code=(
            access.operating_role or access.functional_role or "PC"
        ),
        allowed_actions=[
            "APPROVE_CORRECTION",
            "REJECT_CORRECTION",
            "ADD_COMMENT",
        ],
        completion_protocol="MACHINE_VERIFIED",
    )
    return {
        "documentId": str(document_id),
        "fieldKey": command.fieldKey,
        "applied": False,
        "taskId": str(task_id),
    }
