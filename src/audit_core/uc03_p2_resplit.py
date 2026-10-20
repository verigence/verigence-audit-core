"""Split a file that came in through the old capture screen into pages.

Found 2026-10-20 (journey 9d1c307c, Ramakanta_Sahoo.pdf): a 5-page PDF
uploaded through the old Booking screen went to Document Intelligence as ONE
document, so only one page was ever read, the customer was never named and
the booking stayed stuck. The old screen is closed now; this re-runs such a
file through the Phase 2 upload path without anyone uploading it again:

  1. the original bytes are fetched back from Document Intelligence (Audit
     Core never held them -- the old screen uploaded straight to DI);
  2. they are stored as a new Phase 2 upload batch of the same Journey, and
     the batch is split page by page like any upload from the unified screen;
  3. the batch records the old document as the one it replaces, so the
     whole-file document is retired (SUPERSEDED, kept in history) the moment
     the first page is read -- never before.
"""
from __future__ import annotations

import re
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_engine, get_human_principal
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
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_runtime import enqueue_work, record_activity
from audit_core.uc03_p2_storage import P2DocumentStorageError, get_p2_document_storage

router = APIRouter(
    prefix="/p2/v1/tenants/{tenant_id}/journeys/{journey_id}",
    tags=["uc03-phase2-documents"],
)

# Re-reading a document: PC and TL both hold audit.evidence.refresh (the TL
# bundle has no audit.journey.update).
_UPDATE_PERMISSION = "audit.evidence.refresh"
_ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png"}


def _safe_filename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip() or "document")
    return name[:180] or "document"


def old_screen_documents(
    connection: Any, *, tenant_id: str, journey_id: UUID, open_booking_only: bool = False,
) -> list[dict[str, Any]]:
    """Active documents of the Journey that never went through the Phase 2
    page split (no p2_document_queue row) and have not been re-split yet.
    ``open_booking_only``: only while the Booking is still open -- an older
    Journey whose Booking closed long ago is not offered for splitting."""
    if open_booking_only and connection.execute(
        text(
            "SELECT 1 FROM auditcore.journey_stage_states WHERE tenant_id=:t AND journey_id=:j "
            "AND stage_code='BOOKING' AND business_status IN ('BOOKING_CLOSED','BOOKING_CANCELLED','DUPLICATE_BOOKING')"
        ),
        {"t": tenant_id, "j": journey_id},
    ).first():
        return []
    rows = connection.execute(
        text(
            """
            SELECT e.di_document_id, e.evidence_id, e.document_type_key,
                   d.original_filename, d.content_type, d.created_by_actor_id
            FROM auditcore.evidence e
            LEFT JOIN auditcore.document_capture_v2_documents d
              ON d.tenant_id=e.tenant_id AND d.journey_id=e.journey_id AND d.di_document_id=e.di_document_id
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status='ACTIVE'
              AND e.di_document_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM auditcore.p2_document_queue q
                               WHERE q.tenant_id=e.tenant_id AND q.journey_id=e.journey_id
                                 AND q.di_document_id=e.di_document_id)
              AND NOT EXISTS (SELECT 1 FROM auditcore.p2_upload_batches b
                               WHERE b.tenant_id=e.tenant_id AND b.journey_id=e.journey_id
                                 AND b.replaces_document_id=e.di_document_id
                                 AND b.batch_status NOT IN ('FAILED','CANCELLED'))
            ORDER BY e.linked_at_utc
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/old-screen-documents")
def list_old_screen_documents(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> dict[str, Any]:
    """Files of this Journey that came in through the old screen as one
    document and can be split into pages."""
    with engine.begin() as connection:
        authorize_p2(
            connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
            authorization_client=authorization_client, permission_key="audit.journey.read",
        )
        set_tenant_context(connection, tenant_id)
        rows = old_screen_documents(connection, tenant_id=tenant_id, journey_id=journey_id, open_booking_only=True)
    return {"documents": [
        {"documentId": str(r["di_document_id"]), "filename": r["original_filename"],
         "documentType": r["document_type_key"]}
        for r in rows
    ]}


@router.post("/documents/{document_id}:split-pages", status_code=202)
def split_old_screen_document(
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
) -> dict[str, Any]:
    security_client.get_service_token(audience=_DI_AUDIENCE)
    with engine.begin() as connection:
        access = authorize_p2(
            connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
            authorization_client=authorization_client, permission_key=_UPDATE_PERMISSION,
        )
        set_tenant_context(connection, tenant_id)
        candidates = {
            str(d["di_document_id"]): d
            for d in old_screen_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
        }
        source = candidates.get(str(document_id))
        if source is None:
            raise HTTPException(
                status_code=409,
                detail="This document was already read page by page (or is already being split).",
            )
        context_ref, token = _ensure_di_context(
            connection=connection, engine=engine, tenant_id=tenant_id, journey_id=journey_id,
            security_client=security_client, di_client=di_client,
        )
    try:
        payload, content_type, _ = di_client.get_audit_document_content(
            token=token, tenant_id=tenant_id, external_context_ref=context_ref, document_id=str(document_id),
        )
    except DiClientError as exc:
        raise DependencyUnavailableError(detail="The original file is temporarily unavailable.") from exc
    content_type = (content_type or source.get("content_type") or "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_TYPES:
        raise HTTPException(status_code=422, detail=f"Only PDF, JPEG or PNG files can be split ({content_type}).")

    batch_id = uuid4()
    filename = _safe_filename(source.get("original_filename") or f"{document_id}.pdf")
    object_key = f"p2-documents/{tenant_id}/{journey_id}/{batch_id}/original/{filename}"
    try:
        get_p2_document_storage().put_object(object_key, payload, content_type=content_type)
    except P2DocumentStorageError as exc:
        raise DependencyUnavailableError(detail="Document storage is temporarily unavailable.") from exc

    uploaded_by = str(source.get("created_by_actor_id") or human_principal.subject)
    role = access.operating_role or access.functional_role or "USER"
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        inserted = connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_upload_batches (
                    tenant_id, batch_id, journey_id, client_upload_id,
                    original_filename, content_type, size_bytes, page_count,
                    original_object_key, batch_status, uploaded_by_actor_id, correlation_id,
                    replaces_document_id, replaces_evidence_id
                ) VALUES (
                    :t, :batch, :j, :client, :filename, :content_type, :size, 0,
                    :key, 'UPLOADED', :uploaded_by, :correlation, :doc, :evidence
                )
                ON CONFLICT (tenant_id, journey_id, client_upload_id)
                  WHERE client_upload_id IS NOT NULL
                DO NOTHING
                RETURNING batch_id
                """
            ),
            {"t": tenant_id, "batch": batch_id, "j": journey_id, "client": f"resplit-{document_id}-{batch_id}",
             "filename": filename, "content_type": content_type, "size": len(payload), "key": object_key,
             "uploaded_by": uploaded_by, "correlation": f"resplit-{document_id}", "doc": document_id,
             "evidence": source["evidence_id"]},
        ).scalar_one_or_none()
        if inserted is None:
            raise HTTPException(status_code=409, detail="This document is already being split.")
        enqueue_work(
            connection, tenant_id=tenant_id, journey_id=journey_id, work_type="SPLIT_BATCH",
            work_key=str(batch_id),
            payload={"batchId": str(batch_id), "uploadedBy": uploaded_by, "uploadedByRole": role},
            correlation_id=f"resplit-{document_id}",
        )
        record_activity(
            connection, tenant_id=tenant_id, journey_id=journey_id, event_type="DOCUMENT_SPLIT_INTO_PAGES",
            subject_type="DOCUMENT", subject_id=str(document_id),
            details={
                "batchId": str(batch_id), "filename": source.get("original_filename"), "by": human_principal.subject,
                "role": role, "reason": "Uploaded through the old screen as one document",
            },
            correlation_id=f"resplit-{document_id}",
        )
    return {"batchId": str(batch_id), "documentId": str(document_id), "status": "SPLITTING"}
