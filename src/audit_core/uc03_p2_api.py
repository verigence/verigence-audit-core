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

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.errors import DependencyUnavailableError
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import (
    P2AccessContext,
    authorize_p2,
    check_p2_permission,
    resolve_p2_scope,
)
from audit_core.uc03_p2_controls import control_statistics
from audit_core.uc03_p2_dates import parse_extracted_date
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_runtime import enqueue_work
from audit_core.uc03_p2_stage import (
    condition_reasons,
    read_booking_stage,
    ready_document_counts,
    requirement_items,
)
from audit_core.uc03_p2_storage import (
    P2DocumentStorageError,
    get_p2_document_storage,
)
from audit_core.uc03_p2_submission import upload_status
from audit_core.uc03_p2_tasks import create_p2_task, submit_action, task_queue_tab
from audit_core.uc03_requirement_satisfaction import (
    linked_documents_for_journey,
    requirements_for_journey,
    resolve_requirement_satisfaction,
)

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


def _humanize(key: str) -> str:
    text_ = key.replace("_", " ").strip()
    return text_[:1].upper() + text_[1:]


@router.get("/templates")
def list_templates(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
) -> dict[str, Any]:
    """Document templates for the Web: labels, stage, requirement, page shape,
    key fields and review thresholds. Static per deployment; cacheable."""
    check_p2_permission(
        tenant_id=tenant_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    registry = get_registry()
    templates = []
    for template in registry.documents.values():
        di_fields = registry.di_fields(template.di_types[0]) if template.di_types else []
        templates.append(
            {
                "key": template.key,
                "displayName": template.display_name,
                "stage": template.stage,
                "requirement": template.requirement,
                "condition": template.condition,
                "pageShape": template.pages.shape,
                "maxPages": template.pages.max_pages,
                "diTypes": list(template.di_types),
                "extraction": template.di_schema,
                "reviewThreshold": template.review_threshold,
                "keyFields": list(template.key_fields),
                "fields": [
                    {
                        "key": field["key"],
                        "label": _humanize(field["key"]),
                        "type": field["type"],
                        "required": bool(field["required"]),
                        "isKey": field["key"] in template.key_fields,
                        "reviewThreshold": template.review_threshold_for(field["key"]),
                    }
                    for field in di_fields
                ],
                "journey360": list(template.journey_360),
            }
        )
    return {
        "version": 1,
        "templates": templates,
        "stages": [
            {
                "code": stage.code,
                "states": list(stage.states),
                "gates": [
                    {"key": g.key, "kind": g.kind, "label": g.label, "action": g.missing}
                    for g in stage.gates
                ],
                "completionApproved": stage.completion_approved,
            }
            for stage in sorted(registry.stages.values(), key=lambda item: item.order)
        ],
    }


class P2CreateJourneyCommand(BaseModel):
    outletId: UUID
    # Optional: the customer is named from the documents (PAN, Aadhaar,
    # booking form) as they are read. A name given here is only the
    # entered name until then.
    customerName: str | None = Field(default=None, max_length=200)
    # The name the PC's app shows for the signed-in user, kept on the
    # journey so a Team Lead sees whose booking it is (Audit Core only
    # knows the actor id; the display name lives in Security).
    createdByName: str | None = Field(default=None, max_length=200)


@router.post("/journeys", status_code=201)
def create_p2_journey(
    tenant_id: str,
    command: P2CreateJourneyCommand,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Start a new Booking Journey for a customer at the PC's outlet.

    Reuses the existing, idempotent Create Booking transaction so there is one
    way to create a Journey; the PC then lands on the document workspace and
    everything else is driven by the uploaded documents."""
    from audit_core.uc03_booking_commands import _authorize_security
    from audit_core.uc03_create_booking import (
        _create_context,
        _execute_create_booking_atomic,
    )

    _authorize_security(authorization_client, human_principal=human_principal, tenant_id=tenant_id)
    customer_name = " ".join((command.customerName or "").split())
    # No name yet: the Journey id stands as the entered name until a
    # document names the customer (the placeholder migration 0058 defined).
    chosen_journey_id = uuid4() if not customer_name else None
    if chosen_journey_id is not None:
        customer_name = str(chosen_journey_id)
    set_tenant_context(connection, tenant_id)
    context = _create_context(
        connection, tenant_id=tenant_id, actor_id=human_principal.subject, outlet_id=command.outletId,
    )
    body = _execute_create_booking_atomic(
        connection,
        tenant_id=tenant_id,
        context=context,
        customer_name=customer_name,
        actor_id=human_principal.subject,
        idempotency_key=idempotency_key,
        request_payload={"outletId": str(command.outletId),
                         "customerName": "" if chosen_journey_id is not None else customer_name},
        journey_id=chosen_journey_id,
    )
    journey_id = UUID(str(body["journeyId"]))
    created_by_name = " ".join((command.createdByName or "").split()) or None
    if created_by_name:
        connection.execute(
            text(
                """
                UPDATE auditcore.journeys SET created_by_display_name=:name
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND created_by_display_name IS NULL
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "name": created_by_name},
        )
    _activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="JOURNEY_STARTED",
        subject_type="JOURNEY", subject_id=str(journey_id),
        details={"customerName": None if chosen_journey_id is not None else customer_name},
        correlation_id=None,
    )
    return {"journeyId": str(journey_id), "customerId": str(body["customerId"]), "outletId": str(body["outletId"])}


class P2CancelJourneyCommand(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.post("/journeys/{journey_id}:cancel")
def cancel_p2_journey(
    tenant_id: str,
    journey_id: UUID,
    command: P2CancelJourneyCommand,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Delete a booking that a failed document upload left stuck: it closes
    as cancelled with its documents and history kept, and the Team Lead is
    told. Refused while the booking is complete or nothing has failed."""
    from audit_core.uc03_p2_runtime import note_facts_changed
    from audit_core.uc03_p2_workflow import cancel_stuck_journey

    access = _authorize(
        connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_UPDATE_PERMISSION,
    )
    set_tenant_context(connection, tenant_id)
    outcome = cancel_stuck_journey(
        connection, tenant_id=tenant_id, journey_id=journey_id, actor_id=human_principal.subject,
        actor_role=access.operating_role or access.functional_role, reason=command.reason,
    )
    if outcome == "COMPLETED":
        raise HTTPException(status_code=409, detail="This booking is complete and cannot be deleted.")
    if outcome == "ALREADY_CLOSED":
        raise HTTPException(status_code=409, detail="This booking is already closed.")
    if outcome == "NOT_STUCK":
        raise HTTPException(status_code=409, detail="Nothing has failed on this booking: only a booking whose "
                                                    "document upload failed after the retries can be deleted.")
    note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id, reason="JOURNEY_CANCELLED")
    return {"journeyId": str(journey_id), "status": "BOOKING_CANCELLED"}


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
    state: str = "all",
) -> dict[str, Any]:
    """Bookings / Journey 360 list. ``state``: open (not yet delivered),
    closed (delivered or cancelled) or all."""
    _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=None,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    search = (q or "").strip()
    # Page first, then count: the page of journeys is chosen from indexed
    # columns (newest activity first) and every count below is an indexed
    # per-journey lookup for just that page -- never a tenant-wide
    # aggregation, which is what timed the list out on real data.
    rows = connection.execute(
        text(
            """
            WITH page AS (
                SELECT j.tenant_id, j.journey_id, j.journey_reference, j.customer_id, j.dealer_id,
                       j.outlet_id, j.created_at_utc, j.updated_at_utc,
                       COALESCE(c.legal_name, c.display_name) AS customer_name, c.mobile_last4,
                       d.dealer_name, o.outlet_name,
                       NULLIF(concat_ws(' · ',
                         NULLIF(jp.model_name_snapshot,''),
                         NULLIF(jp.variant_name_snapshot,''),
                         NULLIF(jp.colour_name_snapshot,'')
                       ), '') AS vehicle,
                       j.created_by_display_name AS pc_name
                FROM auditcore.journeys j
                JOIN auditcore.customers c
                  ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
                JOIN auditcore.dealers d
                  ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
                JOIN auditcore.dealer_outlets o
                  ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id AND o.outlet_id=j.outlet_id
                LEFT JOIN auditcore.journey_products jp
                  ON jp.tenant_id=j.tenant_id AND jp.journey_id=j.journey_id
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
                  AND (
                    CAST(:state AS varchar) = 'all'
                    OR (CAST(:state AS varchar) = 'closed') = (
                      EXISTS (SELECT 1 FROM auditcore.journey_stage_states st
                              WHERE st.tenant_id=j.tenant_id AND st.journey_id=j.journey_id
                                AND ((st.stage_code='DELIVERY' AND st.business_completed_at_utc IS NOT NULL)
                                     OR (st.stage_code='BOOKING'
                                         AND (st.business_status IN ('BOOKING_CANCELLED','DUPLICATE_BOOKING')
                                              OR st.closure_disposition='NO_DELIVERY'))))
                      OR EXISTS (SELECT 1 FROM auditcore.deliveries dv
                                 WHERE dv.tenant_id=j.tenant_id AND dv.journey_id=j.journey_id
                                   AND dv.actual_delivered_at IS NOT NULL)
                    )
                  )
                  AND (
                    :search = ''
                    OR c.display_name ILIKE '%' || :search || '%'
                    OR COALESCE(c.legal_name,'') ILIKE '%' || :search || '%'
                    OR d.dealer_name ILIKE '%' || :search || '%'
                    OR o.outlet_name ILIKE '%' || :search || '%'
                    OR COALESCE(jp.model_name_snapshot,'') ILIKE '%' || :search || '%'
                    OR COALESCE(j.journey_reference,'') ILIKE '%' || :search || '%'
                    OR j.journey_id::text ILIKE '%' || :search || '%'
                  )
                ORDER BY j.updated_at_utc DESC, j.journey_id DESC
                LIMIT :limit
            )
            SELECT p.journey_id, p.journey_reference, p.customer_name, p.mobile_last4,
                   p.dealer_name, p.outlet_name, p.vehicle, p.created_at_utc, p.updated_at_utc,
                   p.pc_name, pv.price_variance, gp.gate_pass_date,
                   COALESCE(pr.current_stage,
                     CASE
                       WHEN ds.business_completed_at_utc IS NOT NULL OR dl.actual_delivered_at IS NOT NULL
                         THEN 'DELIVERY_COMPLETE'
                       WHEN ds.journey_id IS NOT NULL OR dl.journey_id IS NOT NULL
                         THEN 'DELIVERY_DOCUMENT_UPLOAD'
                       WHEN bs.business_status='BOOKING_CLOSED' OR bs.booking_confirm_date IS NOT NULL
                         THEN 'BOOKING_COMPLETE'
                       ELSE 'BOOKING_DOCUMENT_UPLOAD'
                     END) AS current_stage,
                   COALESCE(pr.booking_completion_state, 'IN_PROGRESS') AS booking_completion_state,
                   COALESCE(pr.delivery_completion_state, 'IN_PROGRESS') AS delivery_completion_state,
                   COALESCE(pr.booking_receipt_total, pay.booking_total, 0) AS booking_receipt_total,
                   pr.booking_minimum_amount,
                   COALESCE(pr.manual_verification_pending_count,0) AS manual_verification_pending_count,
                   COALESCE(bs.booking_confirm_date, bs.booking_confirmed_at_utc::date) AS booking_confirm_date,
                   dl.actual_delivered_at AS delivered_at,
                   dl.planned_delivery_at AS planned_delivery_at,
                   (pr.journey_id IS NOT NULL) AS phase2,
                   (ds.business_completed_at_utc IS NOT NULL OR dl.actual_delivered_at IS NOT NULL
                    OR bs.business_status IN ('BOOKING_CANCELLED','DUPLICATE_BOOKING')
                    OR bs.closure_disposition='NO_DELIVERY') AS closed,
                   (bs.business_status IN ('BOOKING_CANCELLED','DUPLICATE_BOOKING')
                    OR bs.closure_disposition='NO_DELIVERY') AS cancelled,
                   bs.business_status AS booking_status,
                   CASE WHEN bs.business_status='BOOKING_CLOSED' THEN bs.business_completed_at_utc END
                     AS booking_completed_at,
                   COALESCE(ds.business_completed_at_utc, dl.actual_delivered_at) AS delivery_completed_at,
                   (SELECT jr.review_completed_at_utc FROM auditcore.journeys jr
                     WHERE jr.tenant_id=p.tenant_id AND jr.journey_id=p.journey_id) AS delivery_reviewed_at,
                   bs.capture_completed_at_utc AS booking_submitted_at,
                   ds.capture_completed_at_utc AS delivery_submitted_at,
                   ev.documents, pq.failed_pages, pq.retrying_pages,
                   tk.total_tasks, tk.open_tasks, tk.overdue_tasks, tk.pc_open_tasks, tk.tl_open_tasks,
                   fd.open_findings
            FROM page p
            LEFT JOIN auditcore.p2_journey_runtime pr
              ON pr.tenant_id=p.tenant_id AND pr.journey_id=p.journey_id
            LEFT JOIN auditcore.journey_stage_states bs
              ON bs.tenant_id=p.tenant_id AND bs.journey_id=p.journey_id AND bs.stage_code='BOOKING'
            LEFT JOIN auditcore.journey_stage_states ds
              ON ds.tenant_id=p.tenant_id AND ds.journey_id=p.journey_id AND ds.stage_code='DELIVERY'
            LEFT JOIN auditcore.deliveries dl
              ON dl.tenant_id=p.tenant_id AND dl.journey_id=p.journey_id
            LEFT JOIN LATERAL (
              SELECT COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0) AS booking_total
              FROM auditcore.payments
              WHERE tenant_id=p.tenant_id AND journey_id=p.journey_id
                AND COALESCE(payment_stage, 'BOOKING')='BOOKING'
            ) pay ON true
            LEFT JOIN LATERAL (
              SELECT COUNT(*) AS documents FROM auditcore.evidence
              WHERE tenant_id=p.tenant_id AND journey_id=p.journey_id AND association_status='ACTIVE'
            ) ev ON true
            LEFT JOIN LATERAL (
              -- pages whose processing failed for good, and pages waiting for a retry
              SELECT COUNT(*) FILTER (WHERE q.queue_status IN ('FAILED','DEAD_LETTER'))
                     + (SELECT COUNT(*) FROM auditcore.p2_upload_batches b
                         WHERE b.tenant_id=p.tenant_id AND b.journey_id=p.journey_id AND b.batch_status='FAILED') AS failed_pages,
                     COUNT(*) FILTER (WHERE q.queue_status='RETRY_WAIT') AS retrying_pages
              FROM auditcore.p2_document_queue q
              WHERE q.tenant_id=p.tenant_id AND q.journey_id=p.journey_id
            ) pq ON true
            LEFT JOIN LATERAL (
              SELECT COUNT(*) AS total_tasks,
                     COUNT(*) FILTER (WHERE is_open) AS open_tasks,
                     COUNT(*) FILTER (WHERE is_open AND due_at_utc < now()) AS overdue_tasks,
                     COUNT(*) FILTER (WHERE is_open AND role='PC') AS pc_open_tasks,
                     COUNT(*) FILTER (WHERE is_open AND role IN ('TL','PM')) AS tl_open_tasks
              FROM (
                SELECT due_at_utc, assigned_role_code AS role,
                       task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER') AS is_open
                FROM auditcore.p2_tasks
                WHERE tenant_id=p.tenant_id AND journey_id=p.journey_id
                UNION ALL
                SELECT due_at_utc, assigned_role_code AS role,
                       task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT') AS is_open
                FROM auditcore.workflow_tasks
                WHERE tenant_id=p.tenant_id AND journey_id=p.journey_id
                  AND assigned_role_code IN ('PC','TL','PM','EXECUTIVE')
                  AND pr.journey_id IS NULL
              ) t
            ) tk ON true
            LEFT JOIN LATERAL (
              SELECT COUNT(*) AS open_findings FROM auditcore.audit_findings
              WHERE tenant_id=p.tenant_id AND journey_id=p.journey_id
                AND finding_status IN ('OPEN','ACKNOWLEDGED')
            ) fd ON true
            LEFT JOIN LATERAL (
              -- Journey 360's "current vs standard": for every priced line the
              -- figure a document now carries (the invoice, else the booking
              -- form, else the line's own actual) against the price list, less
              -- the same difference on the discounts, over the lines both sides
              -- know. Nothing priced or nothing different is 0, never blank.
              SELECT COALESCE((
                SELECT SUM(cur.current - cl.standard_amount)
                FROM auditcore.commercial_lines cl
                LEFT JOIN LATERAL (
                  SELECT COALESCE(
                    (SELECT s.amount FROM auditcore.commercial_line_source_values s
                      WHERE s.tenant_id=cl.tenant_id AND s.journey_id=cl.journey_id
                        AND s.line_kind='COMMERCIAL' AND s.component_key=cl.component_key
                        AND s.source_document_type NOT IN ('booking_form','booking_docket','order_taking_form',
                                                           'customer_ledger','cost_sheet')
                      ORDER BY s.updated_at_utc DESC LIMIT 1),
                    (SELECT s.amount FROM auditcore.commercial_line_source_values s
                      WHERE s.tenant_id=cl.tenant_id AND s.journey_id=cl.journey_id
                        AND s.line_kind='COMMERCIAL' AND s.component_key=cl.component_key
                        AND s.source_document_type IN ('booking_form','booking_docket','order_taking_form')
                      ORDER BY s.updated_at_utc DESC LIMIT 1),
                    cl.actual_amount) AS current
                ) cur ON true
                WHERE cl.tenant_id=p.tenant_id AND cl.journey_id=p.journey_id
                  AND cl.standard_amount IS NOT NULL AND cur.current IS NOT NULL
              ), 0)
              - COALESCE((
                SELECT SUM(da.actual_discount_amount - da.standard_eligible_amount)
                FROM auditcore.discount_applications da
                WHERE da.tenant_id=p.tenant_id AND da.journey_id=p.journey_id
                  AND da.actual_discount_amount IS NOT NULL AND da.standard_eligible_amount IS NOT NULL
              ), 0) AS price_variance
            ) pv ON true
            LEFT JOIN LATERAL (
              -- The delivery date is the one printed on the gate pass.
              SELECT f.effective_value AS gate_pass_date
              FROM auditcore.evidence e
              JOIN auditcore.journey_document_extracted_fields f
                ON f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id AND f.di_document_id=e.di_document_id
              WHERE e.tenant_id=p.tenant_id AND e.journey_id=p.journey_id AND e.association_status='ACTIVE'
                AND e.document_type_key='gate_pass' AND f.field_key='delivery_date'
                AND f.effective_value IS NOT NULL AND f.effective_value <> 'null'::jsonb
                AND f.effective_value <> '""'::jsonb
              ORDER BY e.linked_at_utc DESC LIMIT 1
            ) gp ON true
            ORDER BY p.updated_at_utc DESC, p.journey_id DESC
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": human_principal.subject,
            "search": search,
            "limit": min(max(limit, 1), 250),
            "state": state if state in {"open", "closed", "all"} else "all",
        },
    ).mappings().all()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["journey_id"] = str(item["journey_id"])
        for key in ("documents", "total_tasks", "open_tasks", "overdue_tasks", "open_findings",
                    "pc_open_tasks", "tl_open_tasks"):
            item[key] = int(item.get(key) or 0)
        for key in ("booking_receipt_total", "booking_minimum_amount", "price_variance"):
            if item.get(key) is not None:
                item[key] = str(item[key])
        item["price_variance"] = item.get("price_variance") or "0"
        gate_pass = parse_extracted_date(item.pop("gate_pass_date", None))
        if gate_pass is not None:
            item["delivered_at"] = gate_pass.isoformat()
        items.append(item)
    return {"items": items}


@router.get("/journeys:summary")
def journeys_summary(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """What matters on the Booking & Delivery screen: what is open now, what
    has been closed all-time, how many tasks are open across the journeys in
    scope, and what was started and completed this week and this month
    (India time) with the average time a booking and a delivery took."""
    _authorize(
        connection, tenant_id=tenant_id, journey_id=None, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_READ_PERMISSION,
    )
    row = connection.execute(
        text(
            """
            WITH scoped AS (
              SELECT j.tenant_id, j.journey_id, j.created_at_utc,
                     COALESCE(bs.first_started_at_utc, j.created_at_utc) AS booking_started,
                     CASE WHEN bs.business_status='BOOKING_CLOSED' THEN bs.business_completed_at_utc END
                       AS booking_completed,
                     ds.first_started_at_utc AS delivery_started,
                     COALESCE(ds.business_completed_at_utc, dl.actual_delivered_at) AS delivery_completed,
                     COALESCE(bs.business_status IN ('BOOKING_CANCELLED','DUPLICATE_BOOKING')
                              OR bs.closure_disposition='NO_DELIVERY', false) AS cancelled,
                     (pr.current_stage LIKE 'DELIVERY%' OR ds.journey_id IS NOT NULL OR dl.journey_id IS NOT NULL)
                       AS in_delivery
              FROM auditcore.journeys j
              LEFT JOIN auditcore.p2_journey_runtime pr ON pr.tenant_id=j.tenant_id AND pr.journey_id=j.journey_id
              LEFT JOIN auditcore.journey_stage_states bs
                ON bs.tenant_id=j.tenant_id AND bs.journey_id=j.journey_id AND bs.stage_code='BOOKING'
              LEFT JOIN auditcore.journey_stage_states ds
                ON ds.tenant_id=j.tenant_id AND ds.journey_id=j.journey_id AND ds.stage_code='DELIVERY'
              LEFT JOIN auditcore.deliveries dl ON dl.tenant_id=j.tenant_id AND dl.journey_id=j.journey_id
              WHERE j.tenant_id=:tenant_id
                AND EXISTS (
                  SELECT 1 FROM auditcore.business_assignments ba
                  WHERE ba.tenant_id=j.tenant_id AND ba.security_actor_id=:actor_id
                    AND ba.assignment_status='ACTIVE' AND ba.effective_from <= now()
                    AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                    AND (ba.dealer_id IS NULL OR (ba.dealer_id=j.dealer_id
                         AND (ba.outlet_id IS NULL OR ba.outlet_id=j.outlet_id)))
                )
            ),
            bounds AS (
              SELECT (date_trunc('week', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata') AS week_start,
                     (date_trunc('month', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata') AS month_start
            )
            SELECT
              COUNT(*) FILTER (WHERE booking_completed IS NULL AND NOT cancelled) AS open_bookings,
              COUNT(*) FILTER (WHERE in_delivery AND delivery_completed IS NULL AND NOT cancelled) AS open_deliveries,
              COUNT(*) FILTER (WHERE booking_completed IS NOT NULL) AS closed_bookings,
              COUNT(*) FILTER (WHERE delivery_completed IS NOT NULL) AS closed_deliveries,
              (SELECT COUNT(*) FROM auditcore.p2_tasks t
                WHERE t.tenant_id=:tenant_id
                  AND t.journey_id IN (SELECT s.journey_id FROM scoped s)
                  AND t.task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')) AS open_tasks,
              COUNT(*) FILTER (WHERE booking_started >= b.week_start) AS week_started,
              COUNT(*) FILTER (WHERE booking_completed >= b.week_start) AS week_bookings_completed,
              COUNT(*) FILTER (WHERE delivery_completed >= b.week_start) AS week_deliveries_completed,
              COUNT(*) FILTER (WHERE booking_started >= b.month_start) AS month_started,
              COUNT(*) FILTER (WHERE booking_completed >= b.month_start) AS month_bookings_completed,
              COUNT(*) FILTER (WHERE delivery_completed >= b.month_start) AS month_deliveries_completed,
              AVG(EXTRACT(EPOCH FROM booking_completed - booking_started) / 3600.0)
                FILTER (WHERE booking_completed >= b.month_start) AS month_booking_hours,
              AVG(EXTRACT(EPOCH FROM delivery_completed - COALESCE(delivery_started, booking_completed)) / 3600.0)
                FILTER (WHERE delivery_completed >= b.month_start) AS month_delivery_hours
            FROM scoped, bounds b
            """
        ),
        {"tenant_id": tenant_id, "actor_id": human_principal.subject},
    ).mappings().one()

    def hours(value: Any) -> float | None:
        return round(float(value), 1) if value is not None else None

    return {
        "open": {"bookings": int(row["open_bookings"] or 0), "deliveries": int(row["open_deliveries"] or 0)},
        # All-time closed stages and the open tasks across every journey in
        # scope: the five numbers the Booking & Delivery screen shows.
        "closed": {"bookings": int(row["closed_bookings"] or 0), "deliveries": int(row["closed_deliveries"] or 0)},
        "tasks": {"open": int(row["open_tasks"] or 0)},
        "week": {
            "bookingsStarted": int(row["week_started"] or 0),
            "bookingsCompleted": int(row["week_bookings_completed"] or 0),
            "deliveriesCompleted": int(row["week_deliveries_completed"] or 0),
        },
        "month": {
            "bookingsStarted": int(row["month_started"] or 0),
            "bookingsCompleted": int(row["month_bookings_completed"] or 0),
            "deliveriesCompleted": int(row["month_deliveries_completed"] or 0),
            "avgBookingHours": hours(row["month_booking_hours"]),
            "avgDeliveryHours": hours(row["month_delivery_hours"]),
        },
    }


class UploadInitFile(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    contentType: str = Field(min_length=1, max_length=160)
    sizeBytes: int = Field(gt=0)
    clientUploadId: str = Field(min_length=8, max_length=160)


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
        candidate_batch_id = uuid4()
        safe_name = _safe_filename(item.filename)
        candidate_object_key = (
            f"p2-documents/{tenant_id}/{journey_id}/{candidate_batch_id}/original/{safe_name}"
        )
        inserted = connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_upload_batches (
                    tenant_id, batch_id, journey_id, client_upload_id,
                    original_filename, content_type, size_bytes, page_count,
                    original_object_key, batch_status, uploaded_by_actor_id,
                    correlation_id
                ) VALUES (
                    :tenant_id, :batch_id, :journey_id, :client_upload_id,
                    :filename, :content_type, :size_bytes, 0,
                    :object_key, 'AWAITING_UPLOAD', :actor_id,
                    :correlation_id
                )
                ON CONFLICT (tenant_id, journey_id, client_upload_id)
                  WHERE client_upload_id IS NOT NULL
                DO NOTHING
                RETURNING batch_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "batch_id": candidate_batch_id,
                "journey_id": journey_id,
                "client_upload_id": item.clientUploadId,
                "filename": item.filename,
                "content_type": content_type,
                "size_bytes": item.sizeBytes,
                "object_key": candidate_object_key,
                "actor_id": human_principal.subject,
                "correlation_id": correlation_id,
            },
        ).scalar_one_or_none()

        created = inserted is not None
        if created:
            batch_id = candidate_batch_id
            object_key = candidate_object_key
            batch_status = "AWAITING_UPLOAD"
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
        else:
            existing = connection.execute(
                text(
                    """
                    SELECT batch_id, original_filename, content_type, size_bytes,
                           original_object_key, batch_status
                    FROM auditcore.p2_upload_batches
                    WHERE tenant_id=:tenant_id
                      AND journey_id=:journey_id
                      AND client_upload_id=:client_upload_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "journey_id": journey_id,
                    "client_upload_id": item.clientUploadId,
                },
            ).mappings().one()
            if (
                str(existing["original_filename"]) != item.filename
                or str(existing["content_type"]) != content_type
                or int(existing["size_bytes"]) != item.sizeBytes
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{item.filename}: clientUploadId was already used "
                        "for different file metadata."
                    ),
                )
            batch_id = UUID(str(existing["batch_id"]))
            object_key = str(existing["original_object_key"])
            batch_status = str(existing["batch_status"])

        already_accepted = batch_status != "AWAITING_UPLOAD"
        upload_url: str | None = None
        if not already_accepted:
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

        prepared.append(
            {
                "batchId": str(batch_id),
                "clientUploadId": item.clientUploadId,
                "filename": item.filename,
                "status": batch_status,
                "alreadyAccepted": already_accepted,
                "uploadUrl": upload_url,
                "uploadHeaders": (
                    {"Content-Type": content_type}
                    if upload_url is not None
                    else {}
                ),
                "expiresInSeconds": 900 if upload_url is not None else 0,
            }
        )

    return {"journeyId": str(journey_id), "uploads": prepared}



@router.post("/journeys/{journey_id}/documents/{document_id}/replace")
def replace_document(
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    command: UploadInitFile,
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
    evidence = connection.execute(
        text(
            """
            SELECT evidence_id
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
    if evidence is None:
        raise HTTPException(
            status_code=404,
            detail="The active document to replace was not found.",
        )

    content_type = command.contentType.lower().strip()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"{command.filename}: unsupported content type {command.contentType}.",
        )
    max_bytes = int(os.environ.get("P2_MAX_UPLOAD_BYTES", str(_DEFAULT_MAX_UPLOAD_BYTES)))
    if command.sizeBytes > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"{command.filename}: file exceeds the {max_bytes // (1024 * 1024)} MB limit.",
        )
    try:
        storage = get_p2_document_storage()
    except RuntimeError as exc:
        raise DependencyUnavailableError(
            detail="Phase 2 document storage is not configured."
        ) from exc

    candidate_batch_id = uuid4()
    candidate_key = (
        f"p2-documents/{tenant_id}/{journey_id}/{candidate_batch_id}/original/"
        f"{_safe_filename(command.filename)}"
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_upload_batches (
                tenant_id, batch_id, journey_id, client_upload_id,
                original_filename, content_type, size_bytes, page_count,
                original_object_key, batch_status, uploaded_by_actor_id,
                correlation_id, replaces_document_id, replaces_evidence_id
            ) VALUES (
                :tenant_id, :batch_id, :journey_id, :client_upload_id,
                :filename, :content_type, :size_bytes, 0,
                :object_key, 'AWAITING_UPLOAD', :actor_id,
                :correlation_id, :document_id, :evidence_id
            )
            ON CONFLICT (tenant_id, journey_id, client_upload_id)
              WHERE client_upload_id IS NOT NULL
            DO NOTHING
            """
        ),
        {
            "tenant_id": tenant_id,
            "batch_id": candidate_batch_id,
            "journey_id": journey_id,
            "client_upload_id": command.clientUploadId,
            "filename": command.filename,
            "content_type": content_type,
            "size_bytes": command.sizeBytes,
            "object_key": candidate_key,
            "actor_id": human_principal.subject,
            "correlation_id": get_correlation_id(request),
            "document_id": document_id,
            "evidence_id": evidence["evidence_id"],
        },
    )
    existing = connection.execute(
        text(
            """
            SELECT batch_id, original_object_key, batch_status, replaces_document_id,
                   original_filename, content_type, size_bytes
            FROM auditcore.p2_upload_batches
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND client_upload_id=:client_upload_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "client_upload_id": command.clientUploadId,
        },
    ).mappings().one()
    if (
        existing["replaces_document_id"] != document_id
        or str(existing["original_filename"]) != command.filename
        or str(existing["content_type"]) != content_type
        or int(existing["size_bytes"]) != command.sizeBytes
    ):
        raise HTTPException(
            status_code=409,
            detail=f"{command.filename}: clientUploadId was already used for a different upload.",
        )
    batch_id = UUID(str(existing["batch_id"]))
    object_key = str(existing["original_object_key"])
    if existing["batch_status"] != "AWAITING_UPLOAD":
        return {
            "journeyId": str(journey_id),
            "batchId": str(batch_id),
            "clientUploadId": command.clientUploadId,
            "filename": command.filename,
            "status": str(existing["batch_status"]),
            "alreadyAccepted": True,
            "uploadUrl": None,
            "uploadHeaders": {},
            "expiresInSeconds": 0,
            "replacesDocumentId": str(document_id),
        }
    try:
        upload_url = storage.presign_put(
            object_key,
            content_type=content_type,
            expires_seconds=900,
        )
    except P2DocumentStorageError as exc:
        raise DependencyUnavailableError(
            detail="The replacement upload could not be prepared."
        ) from exc

    _activity(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        event_type="DOCUMENT_REPLACEMENT_INITIALIZED",
        subject_type="UPLOAD_BATCH",
        subject_id=str(batch_id),
        details={
            "replacesDocumentId": str(document_id),
            "filename": command.filename,
        },
        correlation_id=get_correlation_id(request),
    )
    return {
        "journeyId": str(journey_id),
        "batchId": str(batch_id),
        "clientUploadId": command.clientUploadId,
        "filename": command.filename,
        "status": "AWAITING_UPLOAD",
        "uploadUrl": upload_url,
        "uploadHeaders": {"Content-Type": content_type},
        "expiresInSeconds": 900,
        "replacesDocumentId": str(document_id),
    }


@router.post("/journeys/{journey_id}/uploads/{batch_id}:finalize", status_code=202)
def finalize_upload(
    tenant_id: str,
    journey_id: UUID,
    batch_id: UUID,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    engine: Annotated[Engine, Depends(get_engine)],
) -> dict[str, Any]:
    """Accept an uploaded object durably.

    1. short read (scope + batch)          -- no locks held afterwards
    2. object-storage HEAD                  -- no transaction open
    3. conditional state transition + work  -- one small transaction
    Only after step 3 commits is 202 returned. Concurrent/retried finalize
    calls are idempotent: exactly one wins the AWAITING_UPLOAD transition."""
    decision = check_p2_permission(
        tenant_id=tenant_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    with engine.begin() as connection:
        access = resolve_p2_scope(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            human_principal=human_principal,
            decision=decision,
        )
        batch = connection.execute(
            text(
                """
                SELECT batch_id, original_filename, size_bytes,
                       original_object_key, batch_status, uploaded_by_actor_id
                FROM auditcore.p2_upload_batches
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND batch_id=:batch_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "batch_id": batch_id},
        ).mappings().one_or_none()
    if batch is None:
        raise HTTPException(status_code=404, detail="Upload batch was not found.")
    if batch["batch_status"] != "AWAITING_UPLOAD":
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
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        accepted = connection.execute(
            text(
                """
                UPDATE auditcore.p2_upload_batches
                SET batch_status='UPLOADED', updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND batch_id=:batch_id
                  AND batch_status='AWAITING_UPLOAD'
                RETURNING batch_id
                """
            ),
            {"tenant_id": tenant_id, "batch_id": batch_id},
        ).scalar_one_or_none()
        if accepted is None:
            current = connection.execute(
                text(
                    "SELECT batch_status FROM auditcore.p2_upload_batches "
                    "WHERE tenant_id=:tenant_id AND batch_id=:batch_id"
                ),
                {"tenant_id": tenant_id, "batch_id": batch_id},
            ).scalar_one()
            return {"batchId": str(batch_id), "status": str(current)}
        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_work_queue (
                    tenant_id, journey_id, work_type, work_key,
                    payload, work_status, correlation_id, requested_version
                ) VALUES (
                    :tenant_id, :journey_id, 'SPLIT_BATCH', :work_key,
                    CAST(:payload AS jsonb), 'PENDING', :correlation_id, 1
                )
                ON CONFLICT (tenant_id, work_type, work_key) DO NOTHING
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



@router.get("/journeys/{journey_id}/uploads/{batch_id}")
def get_upload_batch_status(
    tenant_id: str,
    journey_id: UUID,
    batch_id: UUID,
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
    batch = connection.execute(
        text(
            """
            SELECT batch_id, original_filename, content_type, size_bytes,
                   sha256, page_count, batch_status, grouping_status,
                   created_at_utc, updated_at_utc
            FROM auditcore.p2_upload_batches
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND batch_id=:batch_id
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
    pages = connection.execute(
        text(
            """
            SELECT queue_id, page_number, client_upload_id,
                   di_document_id, classified_document_type, business_stage,
                   queue_status, attempt_count, extracted_field_count,
                   last_error, created_at_utc, updated_at_utc
            FROM auditcore.p2_document_queue
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND batch_id=:batch_id
            ORDER BY page_number
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "batch_id": batch_id,
        },
    ).mappings().all()
    result = dict(batch)
    result["batchId"] = str(result.pop("batch_id"))
    result["pages"] = []
    for raw in pages:
        page = dict(raw)
        page["queueId"] = str(page.pop("queue_id"))
        if page.get("di_document_id") is not None:
            page["diDocumentId"] = str(page.pop("di_document_id"))
        result["pages"].append(page)
    return {"journeyId": str(journey_id), "batch": result}


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
            SELECT b.batch_id, b.original_filename, b.content_type, b.size_bytes,
                   b.sha256, b.page_count, b.batch_status, b.grouping_status,
                   b.created_at_utc, b.updated_at_utc,
                   d.batch_id AS duplicate_of_batch_id, d.original_filename AS duplicate_of_filename,
                   d.created_at_utc AS duplicate_of_at
            FROM auditcore.p2_upload_batches b
            -- a refused upload: the same file as an earlier batch of this Journey
            LEFT JOIN LATERAL (
              SELECT o.batch_id, o.original_filename, o.created_at_utc
              FROM auditcore.p2_upload_batches o
              WHERE b.batch_status='CANCELLED' AND b.page_count=0 AND b.sha256 IS NOT NULL
                AND o.tenant_id=b.tenant_id AND o.journey_id=b.journey_id AND o.sha256=b.sha256
                AND o.batch_id<>b.batch_id AND o.batch_status NOT IN ('FAILED','CANCELLED')
              ORDER BY o.created_at_utc ASC LIMIT 1
            ) d ON true
            WHERE b.tenant_id=:tenant_id AND b.journey_id=:journey_id
            ORDER BY b.created_at_utc DESC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    pages = connection.execute(
        text(
            """
            SELECT queue_id, batch_id, page_number, client_upload_id,
                   di_document_id, classified_document_type, business_stage,
                   queue_status, status_reason, template_key, attempt_count,
                   extracted_field_count, last_error, created_at_utc, updated_at_utc,
                   unit_kind, page_numbers, merged_into_queue_id, group_source
            FROM auditcore.p2_document_queue
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            ORDER BY created_at_utc DESC, page_number
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    registry = get_registry()
    by_batch: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        item = dict(page)
        template = (
            registry.documents.get(item["template_key"])
            if item.get("template_key")
            else None
        )
        item["unitKind"] = item.pop("unit_kind")
        item["pageNumbers"] = list(item.pop("page_numbers") or [item["page_number"]])
        merged_into = item.pop("merged_into_queue_id")
        item["mergedIntoQueueId"] = str(merged_into) if merged_into else None
        item["groupSource"] = item.pop("group_source")
        item["templateKey"] = template.key if template else None
        item["displayName"] = template.display_name if template else None
        item["requirement"] = template.requirement if template else None
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
        duplicate_of = item.pop("duplicate_of_batch_id")
        item["duplicateOf"] = {
            "batchId": str(duplicate_of), "filename": item.pop("duplicate_of_filename"),
            "uploadedAtUtc": item.pop("duplicate_of_at"),
        } if duplicate_of else None
        item.pop("duplicate_of_filename", None)
        item.pop("duplicate_of_at", None)
        units = by_batch.get(batch_key, [])
        # Documents first (grouped units and ungrouped pages); merged pages are
        # listed under their document for page-level detail on demand.
        item["pages"] = units
        item["documents"] = [
            {**unit, "memberPages": [u for u in units if u["mergedIntoQueueId"] == unit["queueId"]]}
            for unit in units
            if unit["queue_status"] != "MERGED"
        ]
        result.append(item)
    evidence_rows = connection.execute(
        text(
            """
            SELECT e.evidence_id, e.di_document_id, e.document_type_key,
                   e.process_area, e.association_status, e.supersedes_evidence_id,
                   e.processing_status_cache, e.verification_status_cache,
                   e.confirmation_status_cache, e.linked_at_utc,
                   d.original_filename
            FROM auditcore.evidence e
            LEFT JOIN auditcore.document_capture_v2_documents d
              ON d.tenant_id=e.tenant_id
             AND d.journey_id=e.journey_id
             AND d.di_document_id=e.di_document_id
            WHERE e.tenant_id=:tenant_id AND e.journey_id=:journey_id
              AND e.association_status IN ('ACTIVE','SUPERSEDED')
            ORDER BY e.linked_at_utc DESC, e.evidence_id DESC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    documents = []
    for raw in evidence_rows:
        doc = dict(raw)
        template = registry.template_for_di_type(
            doc.get("document_type_key"), stage=str(doc.get("process_area") or "").upper() or None,
        )
        doc["templateKey"] = template.key
        doc["displayName"] = template.display_name
        doc["requirement"] = template.requirement
        doc["evidenceId"] = str(doc.pop("evidence_id"))
        doc["documentId"] = str(doc.pop("di_document_id"))
        if doc.get("supersedes_evidence_id") is not None:
            doc["supersedesEvidenceId"] = str(doc.pop("supersedes_evidence_id"))
        else:
            doc.pop("supersedes_evidence_id", None)
            doc["supersedesEvidenceId"] = None
        documents.append(doc)

    reasons = condition_reasons(connection, tenant_id=tenant_id, journey_id=journey_id)
    conditions = set(reasons)
    checklist = []
    ready_counts = ready_document_counts(connection, registry, tenant_id=tenant_id, journey_id=journey_id)
    for stage_code in ("BOOKING", "DELIVERY"):
        for item in requirement_items(connection, registry, tenant_id=tenant_id, journey_id=journey_id,
                                      stage=stage_code, reasons=reasons, ready_counts=ready_counts):
            for template_key in item["templates"]:
                template = registry.documents[template_key]
                ready = ready_counts[template_key]
                status = "RECEIVED" if ready else ("COVERED" if item["received"] else "MISSING")
                checklist.append(
                    {
                        "templateKey": template.key,
                        "displayName": template.display_name,
                        "stage": stage_code,
                        # Conditional documents that evidence made mandatory are mandatory.
                        "requirement": "REQUIRED" if item["required"] else template.requirement,
                        "conditional": template.requirement == "CONDITIONAL",
                        "group": template.group,
                        "groupLabel": item["label"] if template.group else None,
                        "reason": item["reason"],
                        "status": status,
                        "readyCount": ready,
                        "documentIds": [
                            d["documentId"] for d in documents
                            if d["templateKey"] == template.key and d.get("association_status") == "ACTIVE"
                        ],
                    }
                )
    return {
        "journeyId": str(journey_id),
        "batches": result,
        "documents": documents,
        "checklist": checklist,
        "conditions": sorted(conditions),
        **upload_status(connection, tenant_id=tenant_id, journey_id=journey_id),
    }


_RETRYABLE_PAGE_STATES = ("FAILED", "DEAD_LETTER", "NEEDS_REVIEW")
_RETYPEABLE_PAGE_STATES = ("SUPPORTING", "NEEDS_REVIEW", "READY", "FAILED")


def _page_unit(connection: Connection, *, tenant_id: str, journey_id: UUID, queue_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT queue_id, batch_id, page_number, page_numbers, unit_kind, queue_status,
                   client_upload_id, di_document_id, page_object_key, page_sha256, business_stage
            FROM auditcore.p2_document_queue
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND queue_id=:queue_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "queue_id": queue_id},
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Page was not found.")
    return dict(row)


@router.post("/journeys/{journey_id}/pages/{queue_id}:retry", status_code=202)
def retry_page(
    tenant_id: str,
    journey_id: UUID,
    queue_id: UUID,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Send a failed page to document intelligence again as a fresh document."""
    _authorize(
        connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_UPDATE_PERMISSION,
    )
    unit = _page_unit(connection, tenant_id=tenant_id, journey_id=journey_id, queue_id=queue_id)
    if unit["queue_status"] not in _RETRYABLE_PAGE_STATES:
        raise HTTPException(status_code=409, detail="Only failed pages can be retried.")
    base = str(unit["client_upload_id"]).split("~r", 1)[0]
    attempt = connection.execute(
        text("SELECT COUNT(*) FROM auditcore.p2_document_queue WHERE tenant_id=:t AND client_upload_id LIKE :p"),
        {"t": tenant_id, "p": f"{base}~r%"},
    ).scalar_one()
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status='QUEUED', client_upload_id=:client_upload_id, di_document_id=NULL,
                di_state=NULL, di_processing_status=NULL, di_submitted_at_utc=NULL,
                di_processed_seen_at_utc=NULL, status_reason=NULL, last_error=NULL,
                attempt_count=0, extracted_field_count=0, updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND queue_id=:queue_id
            """
        ),
        {"tenant_id": tenant_id, "queue_id": queue_id, "client_upload_id": f"{base}~r{int(attempt) + 1}"},
    )
    enqueue_work(
        connection, tenant_id=tenant_id, journey_id=journey_id, work_type="DOCUMENT_INGEST",
        work_key=str(queue_id), payload={"queueId": str(queue_id), "uploadedBy": human_principal.subject},
        correlation_id=get_correlation_id(request),
    )
    _activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="PAGE_RETRY_REQUESTED",
        subject_type="DOCUMENT_PAGE", subject_id=str(queue_id), details={"attempt": int(attempt) + 1},
        correlation_id=get_correlation_id(request),
    )
    return {"queueId": str(queue_id), "status": "QUEUED"}


class SetPageTypeCommand(BaseModel):
    templateKey: str = Field(min_length=1, max_length=120)


@router.post("/journeys/{journey_id}/pages/{queue_id}:set-type", status_code=202)
def set_page_type(
    tenant_id: str,
    journey_id: UUID,
    queue_id: UUID,
    command: SetPageTypeCommand,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The PC says what a page really is. The page is resubmitted to document
    intelligence with exactly that type; the earlier result is retired."""
    access = _authorize(
        connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_UPDATE_PERMISSION,
    )
    registry = get_registry()
    template = registry.documents.get(command.templateKey)
    if template is None or not template.di_types:
        raise HTTPException(status_code=422, detail="Choose a document type from the checklist.")
    unit = _page_unit(connection, tenant_id=tenant_id, journey_id=journey_id, queue_id=queue_id)
    if unit["queue_status"] not in _RETYPEABLE_PAGE_STATES:
        raise HTTPException(status_code=409, detail="This page is still being processed.")
    pages = list(unit["page_numbers"] or [unit["page_number"]])
    client_upload_id = f"p2t-{unit['batch_id']}-{'-'.join(map(str, pages))}-{template.key}-{queue_id.hex[:8]}"
    new_id = uuid4()
    # Retire the current unit first so a same-page group can be replaced.
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status='MERGED', merged_into_queue_id=:new_id,
                status_reason=:reason, updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND queue_id=:queue_id
            """
        ),
        {"tenant_id": tenant_id, "queue_id": queue_id, "new_id": new_id,
         "reason": f"Re-typed as {template.display_name} by {access.operating_role or 'PC'}."},
    )
    inserted = connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_document_queue (
                tenant_id, queue_id, batch_id, journey_id, page_number, page_numbers, page_sha256,
                page_object_key, client_upload_id, queue_status, unit_kind, group_source,
                candidate_override, classified_document_type, template_key, business_stage,
                type_overridden_by_actor_id
            ) VALUES (
                :tenant_id, :new_id, :batch_id, :journey_id, :page_number, :page_numbers, :sha,
                :object_key, :client_upload_id, 'QUEUED', 'GROUP', 'PC',
                CAST(:candidates AS jsonb), :di_type, :template_key, :stage, :actor
            )
            ON CONFLICT (tenant_id, client_upload_id) DO NOTHING
            RETURNING queue_id
            """
        ),
        {
            "tenant_id": tenant_id, "new_id": new_id, "batch_id": unit["batch_id"], "journey_id": journey_id,
            "page_number": pages[0], "page_numbers": pages, "sha": unit["page_sha256"],
            "object_key": unit["page_object_key"], "client_upload_id": client_upload_id,
            "candidates": json.dumps([template.di_types[0]]), "di_type": template.di_types[0],
            "template_key": template.key, "stage": template.stage if template.stage != "ANY" else None,
            "actor": human_principal.subject,
        },
    ).scalar_one_or_none()
    if inserted is None:
        raise HTTPException(status_code=409, detail="This page is already being re-typed.")
    enqueue_work(
        connection, tenant_id=tenant_id, journey_id=journey_id, work_type="DOCUMENT_INGEST",
        work_key=str(new_id), payload={"queueId": str(new_id), "uploadedBy": human_principal.subject},
        correlation_id=get_correlation_id(request),
    )
    _activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="PAGE_RETYPED",
        subject_type="DOCUMENT_PAGE", subject_id=str(queue_id),
        details={"templateKey": template.key, "newQueueId": str(new_id)},
        correlation_id=get_correlation_id(request),
    )
    return {"queueId": str(new_id), "status": "QUEUED", "templateKey": template.key}


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
    latest: bool = False,
) -> dict[str, Any]:
    """Incremental feed. ``after`` is the last event id seen; ``latest=true``
    returns the newest ``limit`` events newest-first (activity views), and
    ``cursor`` is always the newest event id so a client can start polling
    from "now" instead of replaying history."""
    _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    cursor = connection.execute(
        text(
            "SELECT COALESCE(MAX(event_id), 0) FROM auditcore.p2_activity_events "
            "WHERE tenant_id=:tenant_id AND journey_id=:journey_id"
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()
    if latest:
        rows = connection.execute(
            text(
                """
                SELECT event_id, event_type, subject_type, subject_id,
                       details, created_at_utc
                FROM auditcore.p2_activity_events
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                ORDER BY event_id DESC
                LIMIT :limit
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "limit": min(max(limit, 1), 250)},
        ).mappings().all()
        return {"events": [dict(row) for row in rows], "cursor": int(cursor)}
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
    return {"events": [dict(row) for row in rows], "cursor": int(cursor)}


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



def _p2_control_statistics(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, int]:
    row = connection.execute(
        text(
            """
            SELECT COUNT(*) AS tracked,
                   COUNT(*) FILTER (WHERE control_status='PASS') AS passed,
                   COUNT(*) FILTER (WHERE control_status='FAIL') AS failed,
                   COUNT(*) FILTER (WHERE control_status IN ('WAITING_FOR_FACTS','READY','EVALUATING')) AS waiting,
                   COUNT(*) FILTER (WHERE control_status='RETRY_PENDING') AS retry_pending,
                   COUNT(*) FILTER (WHERE control_status='ERROR_TERMINAL') AS errors
            FROM auditcore.p2_control_state
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()
    return {
        "tracked": int(row["tracked"] or 0),
        "passed": int(row["passed"] or 0),
        "failed": int(row["failed"] or 0),
        "waiting": int(row["waiting"] or 0),
        "retryPending": int(row["retry_pending"] or 0),
        "errors": int(row["errors"] or 0),
    }


def _p2_requirement_statistics(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
) -> tuple[int, int]:
    requirements = requirements_for_journey(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
    )
    documents = linked_documents_for_journey(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
    )
    satisfaction = resolve_requirement_satisfaction(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
        requirements=requirements,
        documents=documents,
    )
    governed = [
        item
        for item in satisfaction.values()
        if item.requirement_level in ("REQUIRED", "CONDITIONAL")
        and item.reason != "NOT_APPLICABLE"
        and not item.is_extension
    ]
    return len(governed), sum(1 for item in governed if item.satisfied)


def _p2_page_statistics(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
) -> tuple[int, int]:
    row = connection.execute(
        text(
            """
            SELECT COUNT(*) AS pages,
                   COUNT(*) FILTER (
                     WHERE queue_status IN ('READY','NEEDS_REVIEW','FAILED','DEAD_LETTER')
                   ) AS processed
            FROM auditcore.p2_document_queue
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND upper(COALESCE(business_stage,''))=:stage_code
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
        },
    ).mappings().one()
    return int(row["pages"] or 0), int(row["processed"] or 0)


def _p2_stage_task_statistics(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
) -> tuple[int, int]:
    row = connection.execute(
        text(
            """
            WITH stage_tasks AS (
              SELECT task_status AS status
              FROM auditcore.p2_tasks
              WHERE tenant_id=:tenant_id
                AND journey_id=:journey_id
                AND upper(COALESCE(reference->>'stage',''))=:stage_code
              UNION ALL
              SELECT wi.status
              FROM auditcore.work_items wi
              JOIN auditcore.work_item_task_detail wtd
                ON wtd.tenant_id=wi.tenant_id
               AND wtd.work_item_id=wi.work_item_id
              WHERE wi.tenant_id=:tenant_id
                AND wi.subject_kind='JOURNEY'
                AND wi.subject_ref=:journey_id
                AND wi.item_kind='EXECUTION_TASK'
                AND upper(COALESCE(wtd.process_area,''))=:stage_code
            )
            SELECT COUNT(*) FILTER (
                     WHERE status NOT IN ('VERIFIED_COMPLETE','CANCELLED','RESOLVED','CLOSED')
                   ) AS open,
                   COUNT(*) FILTER (
                     WHERE status IN ('VERIFIED_COMPLETE','RESOLVED','CLOSED')
                   ) AS completed
            FROM stage_tasks
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
        },
    ).mappings().one()
    return int(row["open"] or 0), int(row["completed"] or 0)


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


    booking_required, booking_received = _p2_requirement_statistics(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="BOOKING",
    )
    delivery_required, delivery_received = _p2_requirement_statistics(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="DELIVERY",
    )
    booking_pages, booking_pages_processed = _p2_page_statistics(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="BOOKING",
    )
    delivery_pages, delivery_pages_processed = _p2_page_statistics(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="DELIVERY",
    )
    booking_tasks_open, booking_tasks_completed = _p2_stage_task_statistics(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="BOOKING",
    )
    delivery_tasks_open, delivery_tasks_completed = _p2_stage_task_statistics(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="DELIVERY",
    )

    operational = connection.execute(
        text(
            """
            SELECT
              (SELECT COUNT(*)
                 FROM auditcore.invoice_review_values i
                WHERE i.tenant_id=:tenant_id AND i.journey_id=:journey_id) AS invoices,
              (SELECT COUNT(*)
                 FROM auditcore.finance_records f
                WHERE f.tenant_id=:tenant_id AND f.journey_id=:journey_id) AS finance_records,
              (SELECT COUNT(*)
                 FROM auditcore.insurance_records i
                WHERE i.tenant_id=:tenant_id AND i.journey_id=:journey_id) AS insurance_records,
              (SELECT COUNT(*)
                 FROM auditcore.vehicle_records v
                WHERE v.tenant_id=:tenant_id AND v.journey_id=:journey_id) AS vehicle_records,
              (SELECT COUNT(*)
                 FROM auditcore.registration_records r
                WHERE r.tenant_id=:tenant_id AND r.journey_id=:journey_id) AS registration_records,
              (SELECT COUNT(*)
                 FROM auditcore.p2_document_queue q
                WHERE q.tenant_id=:tenant_id AND q.journey_id=:journey_id
                  AND q.queue_status IN ('FAILED','DEAD_LETTER')) AS extraction_failures,
              (SELECT COALESCE(SUM(GREATEST(q.attempt_count - 1, 0)),0)
                 FROM auditcore.p2_document_queue q
                WHERE q.tenant_id=:tenant_id AND q.journey_id=:journey_id) AS document_retries,
              (SELECT COALESCE(SUM(GREATEST(w.attempt_count - 1, 0)),0)
                 FROM auditcore.p2_work_queue w
                WHERE w.tenant_id=:tenant_id AND w.journey_id=:journey_id) AS work_retries,
              (SELECT COUNT(*)
                 FROM auditcore.journey_document_extracted_fields f
                WHERE f.tenant_id=:tenant_id AND f.journey_id=:journey_id
                  AND f.is_modified=true) AS corrected_fields
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    controls = _p2_control_statistics(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    by_stage = control_statistics(connection, tenant_id=tenant_id, journey_id=journey_id)

    def stage_controls(stage: str) -> dict[str, int]:
        counts = by_stage[stage]
        return {
            "tracked": counts["total"],
            "passed": counts["pass"],
            "failed": counts["fail"],
            "waiting": counts["waiting"],
            "notApplicable": counts["notApplicable"],
            "retryPending": counts["retry"],
            "errors": counts["error"],
        }
    statistics = {
        "booking": {
            "documentsRequired": booking_required,
            "documentsReceived": booking_received,
            "pages": booking_pages,
            "pagesProcessed": booking_pages_processed,
            "paymentReceipts": int(payments["booking_receipts"] or 0),
            "paymentReceived": str(payments["booking_total"] or 0),
            "minimumPayment": str(booking.get("minimumBookingAmount") or 0),
            "manualVerificationPending": int(
                booking.get("manualVerificationPending") or 0
            ),
            # Stage attribution comes from each control's template phases.
            "controls": stage_controls("BOOKING"),
            "tasksOpen": booking_tasks_open,
            "tasksCompleted": booking_tasks_completed,
        },
        "delivery": {
            "documentsRequired": delivery_required,
            "documentsReceived": delivery_received,
            "pages": delivery_pages,
            "pagesProcessed": delivery_pages_processed,
            "invoices": int(operational["invoices"] or 0),
            "paymentReceipts": int(payments["delivery_receipts"] or 0),
            "financeRecords": int(operational["finance_records"] or 0),
            "insuranceRecords": int(operational["insurance_records"] or 0),
            "vehicleRecords": int(operational["vehicle_records"] or 0),
            "registrationRecords": int(operational["registration_records"] or 0),
            "controls": stage_controls("DELIVERY"),
            "tasksOpen": delivery_tasks_open,
            "tasksCompleted": delivery_tasks_completed,
        },
        "journey": {
            "uploads": int(p2_upload["batches"] or 0),
            # P2 does not yet persist a first-class replacement/reupload event.
            # Superseded evidence is the only durable, non-invented proxy.
            "reuploads": int(document_stats["superseded"] or 0),
            "supersededDocuments": int(document_stats["superseded"] or 0),
            "extractionFailures": int(operational["extraction_failures"] or 0),
            "retries": int(operational["document_retries"] or 0)
                + int(operational["work_retries"] or 0),
            "correctedFields": int(operational["corrected_fields"] or 0),
            "openFindings": int(findings["open"] or 0),
            "totalTasks": int(tasks["total"] or 0),
            "slaBreaches": int(tasks["overdue"] or 0),
            "controls": controls,
        },
    }

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
        "statistics": statistics,
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
    includeLegacy: bool | None = None,
    view: str = "open",
    role: str | None = None,
    tab: str = "all",
) -> dict[str, Any]:
    """P2 worklist across P2 and existing (Phase 1) tasks, grouped by the
    client per Journey.

    Legacy workflow tasks are included by default for Journeys that Phase 2
    has not processed (existing data), so a P2 Journey never shows the same
    issue twice; ``includeLegacy=true`` shows every legacy task, ``false``
    none. ``tab`` narrows to DOCUMENTS or MANUAL_VERIFICATION; counts for
    every tab are always returned."""
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
            SELECT t.task_id, t.journey_id, t.root_task_id, t.parent_task_id,
                   t.round_number, t.task_type, t.category, t.origin_kind,
                   t.source_type, t.source_code, t.title, t.description, t.reference,
                   t.severity, t.priority, t.assigned_role_code, t.assigned_actor_id,
                   t.raised_by_actor_id, t.raised_by_role_code, t.allowed_actions,
                   t.completion_protocol, t.task_status, t.due_at_utc,
                   t.created_at_utc, t.updated_at_utc, t.verified_at_utc,
                   t.completion_result,
                   COALESCE(c.legal_name, c.display_name) AS customer_name,
                   o.outlet_name, jr.journey_reference,
                   NULLIF(concat_ws(' · ', NULLIF(jp.model_name_snapshot,''),
                                           NULLIF(jp.variant_name_snapshot,'')), '') AS vehicle,
                   (SELECT COUNT(*) FROM auditcore.p2_task_events ev
                     WHERE ev.tenant_id=t.tenant_id AND ev.task_id=t.task_id
                       AND ev.comment IS NOT NULL) AS comment_count
            FROM auditcore.p2_tasks t
            JOIN auditcore.journeys jr ON jr.tenant_id=t.tenant_id AND jr.journey_id=t.journey_id
            LEFT JOIN auditcore.customers c ON c.tenant_id=jr.tenant_id AND c.customer_id=jr.customer_id
            LEFT JOIN auditcore.dealer_outlets o
              ON o.tenant_id=jr.tenant_id AND o.dealer_id=jr.dealer_id AND o.outlet_id=jr.outlet_id
            LEFT JOIN auditcore.journey_products jp ON jp.tenant_id=t.tenant_id AND jp.journey_id=t.journey_id
            WHERE t.tenant_id=:tenant_id
              AND (CAST(:journey_id AS uuid) IS NULL OR t.journey_id=CAST(:journey_id AS uuid))
              AND (CAST(:role AS varchar) IS NULL OR t.assigned_role_code=CAST(:role AS varchar))
              AND (
                (CAST(:status AS varchar) IS NOT NULL AND t.task_status=CAST(:status AS varchar))
                OR (CAST(:status AS varchar) IS NULL AND CAST(:view AS varchar)='open'
                    AND t.task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER'))
                OR (CAST(:status AS varchar) IS NULL AND CAST(:view AS varchar)='done'
                    AND t.task_status='VERIFIED_COMPLETE'
                    AND t.verified_at_utc > now() - interval '14 days')
                OR (CAST(:status AS varchar) IS NULL AND CAST(:view AS varchar)='all')
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
            "view": view if view in {"open", "done", "all"} else "open",
            "role": role,
        },
    ).mappings().all()

    legacy_mode = "all" if includeLegacy else ("none" if includeLegacy is False else "unprocessed")
    view_mode = view if view in {"open", "done", "all"} else "open"
    # Existing (Phase 1) human tasks, read from their own table. Background
    # jobs in the same table (rule runs, reconciles) have no human owner and
    # are never shown.
    legacy_rows = [] if legacy_mode == "none" else connection.execute(
        text(
            """
            SELECT
              COALESCE(c.legal_name, c.display_name) AS customer_name,
              o.outlet_name, j.journey_reference,
              NULLIF(concat_ws(' · ', NULLIF(jp.model_name_snapshot,''),
                                      NULLIF(jp.variant_name_snapshot,'')), '') AS vehicle,
              wt.workflow_task_id AS task_id,
              wt.journey_id,
              wt.task_type,
              'SYSTEM' AS origin_kind,
              wt.assigned_role_code,
              wt.assigned_actor_id,
              wt.priority AS priority_rank,
              wt.due_at_utc,
              wt.task_status,
              COALESCE(NULLIF(wt.task_payload->>'title',''), initcap(replace(lower(wt.task_type), '_', ' '))) AS title,
              COALESCE(
                NULLIF(wt.task_payload->>'comment',''),
                NULLIF(wt.task_payload->>'description',''),
                NULLIF(wt.task_payload->>'message',''),
                NULLIF(wt.last_error_summary,'')
              ) AS description,
              COALESCE(wt.severity, 'MEDIUM') AS severity,
              wt.process_area,
              wt.effect_key,
              wt.related_finding_id,
              wt.task_payload AS reference,
              wt.created_at_utc,
              wt.updated_at_utc
            FROM auditcore.workflow_tasks wt
            JOIN auditcore.journeys j
              ON j.tenant_id=wt.tenant_id AND j.journey_id=wt.journey_id
            LEFT JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            LEFT JOIN auditcore.dealer_outlets o
              ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id AND o.outlet_id=j.outlet_id
            LEFT JOIN auditcore.journey_products jp ON jp.tenant_id=j.tenant_id AND jp.journey_id=j.journey_id
            WHERE wt.tenant_id=:tenant_id
              AND wt.journey_id IS NOT NULL
              AND wt.assigned_role_code IN ('PC','TL','PM','EXECUTIVE')
              AND (CAST(:role AS varchar) IS NULL OR wt.assigned_role_code=CAST(:role AS varchar))
              AND (CAST(:journey_id AS uuid) IS NULL OR wt.journey_id=CAST(:journey_id AS uuid))
              AND (
                CAST(:legacy_mode AS varchar)='all'
                OR NOT EXISTS (
                  SELECT 1 FROM auditcore.p2_journey_runtime pr
                  WHERE pr.tenant_id=wt.tenant_id AND pr.journey_id=wt.journey_id
                )
              )
              AND (
                (CAST(:status AS varchar) IS NOT NULL AND wt.task_status=CAST(:status AS varchar))
                OR (CAST(:status AS varchar) IS NULL AND CAST(:view AS varchar)='open'
                    AND wt.task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT'))
                OR (CAST(:status AS varchar) IS NULL AND CAST(:view AS varchar)='done'
                    AND wt.task_status='COMPLETED'
                    AND wt.updated_at_utc > now() - interval '14 days')
                OR (CAST(:status AS varchar) IS NULL AND CAST(:view AS varchar)='all')
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
            ORDER BY wt.due_at_utc NULLS LAST, wt.priority DESC, wt.created_at_utc
            LIMIT 500
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "status": status,
            "actor_id": human_principal.subject,
            "legacy_mode": legacy_mode,
            "view": view_mode,
            "role": role,
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
                "customer_name": raw["customer_name"],
                "outlet_name": raw["outlet_name"],
                "vehicle": raw["vehicle"],
                "journey_reference": raw["journey_reference"],
                "comment_count": 0,
                "process_area": raw["process_area"],
                "related_finding_id": (
                    str(raw["related_finding_id"])
                    if raw["related_finding_id"] is not None
                    else None
                ),
            }
        )

    # Phase 2 worklist order is action-first: priority first, then SLA,
    # then operational state. Keep legacy numeric priority semantics intact
    # rather than translating them into P2 labels.
    p2_priority_order = {"URGENT": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}
    status_order = {
        "RETURNED": 0,
        "READY": 1,
        "IN_PROGRESS": 2,
        "VERIFYING": 3,
        "AWAITING_REQUESTER_REVIEW": 4,
        "FAILED": 5,
        "DEAD_LETTER": 6,
    }

    def _safe_sort(item: dict[str, Any]) -> tuple:
        due = item.get("due_at_utc")
        created = item.get("created_at_utc")
        if item.get("source_system") == "LEGACY":
            # Legacy priority is numeric and higher means more urgent.
            priority_key = (0, -int(item.get("priority_rank") or 0))
        else:
            priority_key = (
                1,
                p2_priority_order.get(str(item.get("priority") or "NORMAL").upper(), 2),
            )
        return (
            priority_key,
            due is None,
            due.isoformat() if hasattr(due, "isoformat") else str(due or ""),
            status_order.get(str(item.get("task_status") or "").upper(), 20),
            created.isoformat() if hasattr(created, "isoformat") else str(created or ""),
        )

    items.sort(key=_safe_sort)
    counts = {"ALL": len(items), "DOCUMENTS": 0, "MANUAL_VERIFICATION": 0}
    for item in items:
        item["queue_tab"] = task_queue_tab(str(item.get("task_type") or ""), str(item.get("category") or ""))
        if item["queue_tab"] in counts:
            counts[item["queue_tab"]] += 1
    wanted = tab.upper()
    if wanted in {"DOCUMENTS", "MANUAL_VERIFICATION"}:
        items = [item for item in items if item["queue_tab"] == wanted]
    return {
        "items": items[:500],
        "counts": counts,
        "sources": {
            "p2": len(p2_rows),
            "legacy": len(legacy_rows),
        },
    }




@router.get("/tasks/{task_id}")
def get_task(
    tenant_id: str,
    task_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """One task with its full history (actions, comments, verification)."""
    decision = check_p2_permission(
        tenant_id=tenant_id, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_READ_PERMISSION,
    )
    set_tenant_context(connection, tenant_id)
    task = connection.execute(
        text("SELECT * FROM auditcore.p2_tasks WHERE tenant_id=:t AND task_id=:id"),
        {"t": tenant_id, "id": task_id},
    ).mappings().one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task was not found.")
    resolve_p2_scope(
        connection, tenant_id=tenant_id, journey_id=UUID(str(task["journey_id"])),
        human_principal=human_principal, decision=decision,
    )
    events = connection.execute(
        text(
            """
            SELECT task_event_id, event_type, actor_id, actor_role_code, comment, details, created_at_utc
            FROM auditcore.p2_task_events
            WHERE tenant_id=:t AND task_id=:id
            ORDER BY task_event_id
            """
        ),
        {"t": tenant_id, "id": task_id},
    ).mappings().all()
    item = dict(task)
    for key in ("task_id", "journey_id", "root_task_id", "parent_task_id"):
        if item.get(key) is not None:
            item[key] = str(item[key])
    item["events"] = [dict(event) for event in events]
    return item


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
    decision = check_p2_permission(
        tenant_id=tenant_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    set_tenant_context(connection, tenant_id)
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
    access = resolve_p2_scope(
        connection,
        tenant_id=tenant_id,
        journey_id=UUID(str(task_journey_id)),
        human_principal=human_principal,
        decision=decision,
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
