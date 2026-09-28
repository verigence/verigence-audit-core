"""Phase 2 Journey stage engine, driven by the P2 templates.

Stage gates come from ``p2_templates/document_templates.yaml`` (see
uc03_p2_registry). The engine reads existing business state and writes only
p2_* runtime rows:

- DOCUMENT_READY   an ACTIVE document of the template's type has durable facts
- PAYMENT_MINIMUM  eligible (non-duplicate) receipts reach the tenant minimum.
                   Receipts are counted chronologically across both stages
                   (blueprint 20.2): the prefix that first meets the minimum is
                   the Booking payment, so the gate is met exactly when the
                   eligible total reaches the minimum. Receipts without a date
                   still count as money received.
- NO_OPEN_TASKS    no open task of the listed types (legacy or P2)

Booking completes automatically when every gate passes and reopens
deterministically if a later correction invalidates a gate. Delivery uses the
same framework; its completion criteria stay unconfigured until approved
(blueprint 20.3), but its document readiness is reported.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_booking_confirmation_rules import _minimum_booking_amount
from audit_core.uc03_duplicate_receipt_detection import (
    ReceiptRecord,
    compute_duplicate_groups,
    normalize_receipt_date,
    normalize_receipt_number,
)
from audit_core.uc03_p2_registry import DocumentTemplate, Gate, Registry, get_registry
from audit_core.uc03_p2_runtime import record_activity

_OPEN_LEGACY_TASK_STATUSES = ("PENDING", "READY", "CLAIMED", "IN_PROGRESS", "RETRY_WAIT")
_OPEN_P2_TASK_STATUSES = (
    "READY", "IN_PROGRESS", "ACTION_COMPLETED", "VERIFYING",
    "AWAITING_REQUESTER_REVIEW", "RETURNED",
)
# Legacy evidence rows may carry the pre-canonical key for the same document.
_LEGACY_TYPE_ALIASES = {"booking_form": ("booking_docket",), "dealer_receipt": ("payment_receipt",)}


def _evidence_types(template: DocumentTemplate) -> list[str]:
    types: list[str] = [template.key]
    for di_type in template.di_types:
        types.append(di_type)
        types.extend(_LEGACY_TYPE_ALIASES.get(di_type, ()))
    return list(dict.fromkeys(types))


def ready_document_count(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    template: DocumentTemplate,
) -> int:
    return int(
        connection.execute(
            text(
                """
                SELECT COUNT(DISTINCT e.di_document_id)
                FROM auditcore.evidence e
                WHERE e.tenant_id=:tenant_id
                  AND e.journey_id=:journey_id
                  AND e.association_status='ACTIVE'
                  AND e.document_type_key = ANY(:document_types)
                  AND EXISTS (
                    SELECT 1
                    FROM auditcore.journey_document_extracted_fields f
                    WHERE f.tenant_id=e.tenant_id
                      AND f.journey_id=e.journey_id
                      AND f.di_document_id=e.di_document_id
                      AND f.effective_value IS NOT NULL
                  )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "document_types": _evidence_types(template),
            },
        ).scalar_one()
        or 0
    )


def eligible_receipt_total(connection: Connection, *, tenant_id: str, journey_id: UUID) -> tuple[Decimal, int, int]:
    """(total, counted receipts, excluded duplicates) over every receipt-backed payment."""
    payments = connection.execute(
        text(
            """
            SELECT p.payment_id, p.amount, p.receipt_date, p.receipt_number
            FROM auditcore.payments p
            WHERE p.tenant_id=:tenant_id
              AND p.journey_id=:journey_id
              AND p.status_source='EVIDENCE'
              AND p.amount IS NOT NULL
              AND p.amount > 0
              -- a voided or superseded receipt document no longer counts
              AND EXISTS (
                SELECT 1 FROM auditcore.evidence e
                WHERE e.tenant_id=p.tenant_id AND e.journey_id=p.journey_id
                  AND e.di_document_id=p.source_di_document_id
                  AND e.association_status='ACTIVE'
              )
            ORDER BY p.receipt_date ASC NULLS LAST, p.payment_id ASC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    records = [
        ReceiptRecord(
            document_id=payment["payment_id"],
            stage_code="BOOKING",
            document_type_key="dealer_receipt",
            receipt_number=normalize_receipt_number(payment["receipt_number"]),
            amount=Decimal(str(payment["amount"])),
            receipt_date=normalize_receipt_date(payment["receipt_date"]),
        )
        for payment in payments
    ]
    excluded: set[Any] = set()
    for group in compute_duplicate_groups(records):
        for duplicate in group.documents[1:]:
            excluded.add(duplicate.document_id)
    total = Decimal(0)
    counted = 0
    for payment in payments:
        if payment["payment_id"] in excluded:
            continue
        total += Decimal(str(payment["amount"]))
        counted += 1
    return total, counted, len(excluded)


def open_task_count(connection: Connection, *, tenant_id: str, journey_id: UUID, task_types: tuple[str, ...]) -> int:
    row = connection.execute(
        text(
            """
            SELECT
              (SELECT COUNT(*) FROM auditcore.workflow_tasks
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND task_type = ANY(:task_types) AND task_status = ANY(:legacy_open))
            + (SELECT COUNT(*) FROM auditcore.p2_tasks
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND task_type = ANY(:task_types) AND task_status = ANY(:p2_open))
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "task_types": list(task_types),
            "legacy_open": list(_OPEN_LEGACY_TASK_STATUSES),
            "p2_open": list(_OPEN_P2_TASK_STATUSES),
        },
    ).scalar_one()
    return int(row or 0)


def unreviewed_fields(
    connection: Connection,
    registry: Registry,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage: str | None = None,
    document_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """Low-confidence fields on ACTIVE documents that nobody has reviewed.

    A field needs review when its confidence is below its template threshold
    (strict fields use the stricter bar; missing confidence is never trusted),
    it has a value, and it has not been confirmed or corrected."""
    rows = connection.execute(
        text(
            """
            SELECT f.di_document_id, f.field_key, f.source_canonical_field_id,
                   f.source_fact_version, f.confidence_score, f.effective_value,
                   e.document_type_key, e.process_area
            FROM auditcore.journey_document_extracted_fields f
            JOIN auditcore.evidence e
              ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id
             AND e.di_document_id=f.di_document_id AND e.association_status='ACTIVE'
            WHERE f.tenant_id=:tenant_id AND f.journey_id=:journey_id
              AND f.reviewed_at_utc IS NULL
              AND f.is_modified = false
              AND f.effective_value IS NOT NULL
              AND f.effective_value <> 'null'::jsonb
              AND f.effective_value <> '""'::jsonb
              AND (CAST(:document_id AS uuid) IS NULL OR f.di_document_id=CAST(:document_id AS uuid))
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "document_id": document_id},
    ).mappings().all()
    pending: list[dict[str, Any]] = []
    for row in rows:
        area = str(row["process_area"] or "").upper() or None
        template = registry.template_for_di_type(row["document_type_key"], stage=area)
        if template.is_supporting:
            continue
        if stage is not None and template.stage not in (stage, "ANY"):
            continue
        confidence = float(row["confidence_score"]) if row["confidence_score"] is not None else None
        if not template.needs_review(str(row["field_key"]), confidence):
            continue
        pending.append(
            {
                "documentId": str(row["di_document_id"]),
                "templateKey": template.key,
                "documentName": template.display_name,
                "fieldKey": str(row["field_key"]),
                "canonicalFieldId": row["source_canonical_field_id"],
                "sourceFactVersion": int(row["source_fact_version"] or 1),
                "confidence": confidence,
                "threshold": template.review_threshold_for(str(row["field_key"])),
            }
        )
    return pending


# Discounts whose claim requires a proof document (same keys as the deal
# reconciliation's conditional-evidence check).
_CLAIM_CONDITIONS = {
    "CORPORATE_CUSTOMER": ("CORPORATE_PRIVILEGE", "CORPORATE", "corporate_discount_amount", "corporate_offer_amount"),
    "EXCHANGE_TAKEN": ("EXCHANGE_BONUS", "EXCHANGE", "exchange_discount_amount", "exchange_claim_amount", "bonus_amount"),
    "SCRAPPAGE_CLAIMED": ("SCRAPPAGE_BONUS_DEALER", "SCRAPPAGE_BONUS_COD", "scrappage_discount_amount"),
}
_CLAIM_REASONS = {
    "CORPORATE_CUSTOMER": "The deal claims a corporate discount.",
    "EXCHANGE_TAKEN": "The deal claims an exchange bonus.",
    "SCRAPPAGE_CLAIMED": "The deal claims a scrappage bonus.",
}


def condition_reasons(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, str]:
    """Conditional requirements that apply and why: a PC declaration, an
    observed fact (corporate customer, trade-in on file) or a discount the
    booking form / invoice claims -- a claimed corporate discount makes the
    Corporate ID a required document even for an individual customer."""
    declared = connection.execute(
        text(
            """
            SELECT condition_key, applicable
            FROM auditcore.document_capture_v2_declarations
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    declared_map = {str(row["condition_key"]): bool(row["applicable"]) for row in declared}
    facts = connection.execute(
        text(
            """
            SELECT
              EXISTS (
                SELECT 1 FROM auditcore.journeys j
                JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
                WHERE j.tenant_id=:tenant_id AND j.journey_id=:journey_id
                  AND upper(c.customer_type_code) IN ('CORPORATE','COMPANY','BUSINESS')
              ) AS corporate,
              EXISTS (
                SELECT 1 FROM auditcore.trade_in_cases t
                WHERE t.tenant_id=:tenant_id AND t.journey_id=:journey_id
              ) AS exchange,
              ARRAY(
                SELECT DISTINCT discount_key FROM auditcore.discount_applications
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND COALESCE(actual_discount_amount, 0) > 0
                UNION
                SELECT DISTINCT component_key FROM auditcore.commercial_line_source_values
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND COALESCE(amount, 0) > 0
              ) AS claimed
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()
    claimed = {str(k) for k in (facts["claimed"] or [])}
    reasons: dict[str, str] = {}
    for condition, keys in _CLAIM_CONDITIONS.items():
        if claimed.intersection(keys):
            reasons[condition] = _CLAIM_REASONS[condition]
    if facts["corporate"]:
        reasons.setdefault("CORPORATE_CUSTOMER", "The customer is a company.")
    if facts["exchange"]:
        reasons.setdefault("EXCHANGE_TAKEN", "An exchange vehicle is on file.")
    # A PC declaration decides either way when present.
    for declaration, condition in (("corporateCustomer", "CORPORATE_CUSTOMER"), ("exchangeTaken", "EXCHANGE_TAKEN")):
        if declaration in declared_map:
            if declared_map[declaration]:
                reasons.setdefault(condition, "Declared on the booking.")
            elif condition in reasons and not claimed.intersection(_CLAIM_CONDITIONS[condition]):
                reasons.pop(condition)
    return reasons


def active_conditions(connection: Connection, *, tenant_id: str, journey_id: UUID) -> set[str]:
    """Conditional requirements that apply (see condition_reasons)."""
    return set(condition_reasons(connection, tenant_id=tenant_id, journey_id=journey_id))


def _evaluate_gate(
    connection: Connection,
    registry: Registry,
    gate: Gate,
    *,
    tenant_id: str,
    journey_id: UUID,
    minimum: Decimal,
) -> dict[str, Any]:
    if gate.kind == "DOCUMENT_READY":
        counts = {
            key: ready_document_count(
                connection, tenant_id=tenant_id, journey_id=journey_id, template=registry.document(key),
            )
            for key in gate.documents
        }
        passed = all(count > 0 for count in counts.values())
        return {"passed": passed, "documentCount": sum(counts.values()), "documents": counts}
    if gate.kind == "PAYMENT_MINIMUM":
        total, counted, duplicates = eligible_receipt_total(
            connection, tenant_id=tenant_id, journey_id=journey_id,
        )
        return {
            "passed": total >= minimum,
            "receiptTotal": str(total),
            "minimumAmount": str(minimum),
            "receiptCount": counted,
            "duplicateReceiptsExcluded": duplicates,
            "shortfall": str(max(minimum - total, Decimal(0))),
        }
    if gate.kind == "FIELDS_REVIEWED":
        pending = unreviewed_fields(
            connection, registry, tenant_id=tenant_id, journey_id=journey_id, stage="BOOKING",
        )
        documents = sorted({item["documentName"] for item in pending})
        return {"passed": not pending, "pendingCount": len(pending), "documents": documents}
    if gate.kind == "NO_OPEN_TASKS":
        pending = open_task_count(
            connection, tenant_id=tenant_id, journey_id=journey_id, task_types=gate.task_types,
        )
        return {"passed": pending == 0, "pendingCount": pending}
    raise ValueError(f"Unsupported gate kind {gate.kind}")


def _upsert_gate(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    gate_key: str,
    passed: bool,
    details: dict[str, Any],
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_stage_gate_state (
                tenant_id, journey_id, stage_code, gate_key,
                gate_status, details, evaluated_at_utc
            ) VALUES (
                :tenant_id, :journey_id, :stage_code, :gate_key,
                :gate_status, CAST(:details AS jsonb), now()
            )
            ON CONFLICT (tenant_id, journey_id, stage_code, gate_key)
            DO UPDATE SET gate_status=EXCLUDED.gate_status,
                          details=EXCLUDED.details,
                          evaluated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "gate_key": gate_key,
            "gate_status": "PASS" if passed else "WAITING",
            "details": json.dumps(details, default=str),
        },
    )


def delivery_readiness(
    connection: Connection,
    registry: Registry,
    *,
    tenant_id: str,
    journey_id: UUID,
    conditions: set[str],
) -> dict[str, Any]:
    required = registry.required_documents("DELIVERY", conditions=conditions)
    received: list[str] = []
    missing: list[dict[str, str]] = []
    for template in required:
        if ready_document_count(connection, tenant_id=tenant_id, journey_id=journey_id, template=template) > 0:
            received.append(template.key)
        else:
            missing.append({"key": template.key, "label": template.display_name})
    # Vehicle photos are plain evidence (never classified or extracted) but
    # Delivery is not ready without them.
    photos = int(connection.execute(
        text(
            """
            SELECT COUNT(*) FROM auditcore.delivery_vehicle_photos
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND deleted_at_utc IS NULL
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one())
    if photos:
        received.append("vehicle_photos")
    else:
        missing.append({"key": "vehicle_photos", "label": "Vehicle photos"})
    return {
        "passed": not missing,
        "requiredCount": len(required) + 1,
        "receivedCount": len(received),
        "missing": missing,
        "vehiclePhotos": photos,
    }


def recompute_journey_stage(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    registry: Registry | None = None,
) -> dict[str, Any]:
    registry = registry or get_registry()
    minimum = _minimum_booking_amount(connection, tenant_id=tenant_id)
    booking = registry.stages["BOOKING"]

    gates: dict[str, dict[str, Any]] = {}
    for gate in booking.gates:
        result = _evaluate_gate(
            connection, registry, gate, tenant_id=tenant_id, journey_id=journey_id, minimum=minimum,
        )
        result.update({"label": gate.label, "kind": gate.kind})
        if not result["passed"]:
            result["action"] = gate.missing
        gates[gate.key] = result
        _upsert_gate(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
            gate_key=gate.key, passed=result["passed"], details=result,
        )

    conditions = active_conditions(connection, tenant_id=tenant_id, journey_id=journey_id)
    delivery = delivery_readiness(
        connection, registry, tenant_id=tenant_id, journey_id=journey_id, conditions=conditions,
    )
    _upsert_gate(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        gate_key="REQUIRED_DOCUMENTS", passed=delivery["passed"],
        details={**delivery, "label": "Delivery documents received", "kind": "READINESS"},
    )

    booking_complete = all(item["passed"] for item in gates.values())
    evidence_or_payment_missing = any(
        not item["passed"] for item in gates.values() if item["kind"] in {"DOCUMENT_READY", "PAYMENT_MINIMUM"}
    )
    manual_pending = sum(
        int(item.get("pendingCount") or 0)
        for item in gates.values() if item["kind"] in {"NO_OPEN_TASKS", "FIELDS_REVIEWED"}
    )
    delivery_started = bool(
        connection.execute(
            text(
                """
                SELECT EXISTS (
                  SELECT 1 FROM auditcore.evidence e
                  WHERE e.tenant_id=:tenant_id AND e.journey_id=:journey_id
                    AND e.association_status='ACTIVE'
                    AND upper(COALESCE(e.process_area,''))='DELIVERY'
                ) OR EXISTS (
                  SELECT 1 FROM auditcore.deliveries d
                  WHERE d.tenant_id=:tenant_id AND d.journey_id=:journey_id
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
    )

    if not booking_complete:
        stage = "BOOKING_DOCUMENT_UPLOAD" if evidence_or_payment_missing else "BOOKING_VERIFY_DOCUMENTS"
        booking_state = "IN_PROGRESS" if evidence_or_payment_missing else "BLOCKED"
    elif not delivery_started:
        stage, booking_state = "BOOKING_COMPLETE", "COMPLETE"
    else:
        booking_state = "COMPLETE"
        stage = "DELIVERY_VERIFY_DOCUMENTS" if delivery["passed"] else "DELIVERY_DOCUMENT_UPLOAD"
    # Delivery completion is never automatic until its criteria are approved.
    delivery_state = "IN_PROGRESS"

    previous = connection.execute(
        text(
            """
            SELECT current_stage, booking_completion_state
            FROM auditcore.p2_journey_runtime
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    total_row = gates.get("MINIMUM_BOOKING_PAYMENT") or {}
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_journey_runtime (
                tenant_id, journey_id, current_stage,
                booking_minimum_amount, booking_receipt_total,
                booking_completion_state, delivery_completion_state,
                manual_verification_pending_count
            ) VALUES (
                :tenant_id, :journey_id, :stage,
                :minimum, :receipt_total, :booking_state, :delivery_state, :manual_pending
            )
            ON CONFLICT (tenant_id, journey_id)
            DO UPDATE SET current_stage=EXCLUDED.current_stage,
                          booking_minimum_amount=EXCLUDED.booking_minimum_amount,
                          booking_receipt_total=EXCLUDED.booking_receipt_total,
                          booking_completion_state=EXCLUDED.booking_completion_state,
                          delivery_completion_state=EXCLUDED.delivery_completion_state,
                          manual_verification_pending_count=EXCLUDED.manual_verification_pending_count,
                          updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage": stage,
            "minimum": minimum,
            "receipt_total": Decimal(str(total_row.get("receiptTotal") or 0)),
            "booking_state": booking_state,
            "delivery_state": delivery_state,
            "manual_pending": manual_pending,
        },
    )
    result = {
        "stage": stage,
        "bookingCompletionState": booking_state,
        "deliveryCompletionState": delivery_state,
        "minimumBookingAmount": str(minimum),
        "bookingReceiptTotal": str(total_row.get("receiptTotal") or "0"),
        "manualVerificationPending": manual_pending,
        "gates": gates,
        "delivery": delivery,
        "conditions": sorted(conditions),
    }
    if booking_state == "COMPLETE":
        # Recorded on the existing journey workflow (idempotent).
        from audit_core.uc03_p2_workflow import mark_booking_completed

        mark_booking_completed(connection, tenant_id=tenant_id, journey_id=journey_id)
    if previous is None or previous["current_stage"] != stage or previous["booking_completion_state"] != booking_state:
        record_activity(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="STAGE_CHANGED",
            subject_type="JOURNEY",
            subject_id=str(journey_id),
            details={
                "from": previous["current_stage"] if previous else None,
                "to": stage,
                "bookingCompletionState": booking_state,
            },
        )
    return result


# Backwards-compatible name used by the worker and API.
recompute_booking_stage = recompute_journey_stage


def read_booking_stage(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, Any]:
    registry = get_registry()
    runtime = connection.execute(
        text(
            """
            SELECT current_stage, booking_completion_state, delivery_completion_state,
                   booking_minimum_amount, booking_receipt_total,
                   manual_verification_pending_count, fact_version
            FROM auditcore.p2_journey_runtime
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    gate_rows = connection.execute(
        text(
            """
            SELECT stage_code, gate_key, gate_status, details
            FROM auditcore.p2_stage_gate_state
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    stored = {
        (str(row["stage_code"]), str(row["gate_key"])): {
            **dict(row["details"] or {}),
            "passed": str(row["gate_status"]) == "PASS",
        }
        for row in gate_rows
    }
    gates: dict[str, dict[str, Any]] = {}
    for gate in registry.stages["BOOKING"].gates:
        gates[gate.key] = stored.get(
            ("BOOKING", gate.key),
            {"passed": False, "label": gate.label, "kind": gate.kind, "action": gate.missing},
        )
    delivery = stored.get(("DELIVERY", "REQUIRED_DOCUMENTS"), {"passed": False})
    minimum = (
        runtime["booking_minimum_amount"]
        if runtime is not None and runtime["booking_minimum_amount"] is not None
        else _minimum_booking_amount(connection, tenant_id=tenant_id)
    )
    return {
        "stage": str(runtime["current_stage"]) if runtime else "BOOKING_DOCUMENT_UPLOAD",
        "bookingCompletionState": str(runtime["booking_completion_state"]) if runtime else "IN_PROGRESS",
        "deliveryCompletionState": str(runtime["delivery_completion_state"]) if runtime else "IN_PROGRESS",
        "minimumBookingAmount": str(minimum),
        "bookingReceiptTotal": str(runtime["booking_receipt_total"] or 0) if runtime else "0",
        "manualVerificationPending": int(runtime["manual_verification_pending_count"] or 0) if runtime else 0,
        "factVersion": int(runtime["fact_version"] or 0) if runtime else 0,
        "gates": gates,
        "delivery": delivery,
        "stages": [
            {"code": state, "stage": code}
            for code, template in sorted(registry.stages.items(), key=lambda item: item[1].order)
            for state in template.states
        ],
    }
