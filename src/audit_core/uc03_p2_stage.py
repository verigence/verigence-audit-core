"""Phase 2 journey-stage evaluation.

This module is read-from-existing-business-state / write-to-p2-runtime only.
It does not mutate legacy UC03 stage rows. Booking completion is deliberately
limited to the product rule explicitly agreed for Phase 2:

- Booking Form extracted
- PAN extracted
- Aadhaar extracted
- non-duplicate BOOKING receipts aggregate >= configured minimum booking amount
- no open manual-verification work

Delivery gates are intentionally left configurable/unevaluated until their
business completion contract is approved.
"""
from __future__ import annotations

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

_BOOKING_DOC_GATES = {
    "BOOKING_FORM_EXTRACTED": ("booking_form", "booking_docket"),
    "PAN_EXTRACTED": ("pan_card", "pan"),
    "AADHAAR_EXTRACTED": ("aadhaar",),
}
_OPEN_LEGACY_MANUAL_TASKS = ("PENDING", "READY", "CLAIMED", "IN_PROGRESS", "RETRY_WAIT")
_OPEN_P2_MANUAL_TASKS = (
    "READY", "IN_PROGRESS", "ACTION_COMPLETED", "VERIFYING",
    "AWAITING_REQUESTER_REVIEW", "RETURNED",
)


def _document_extracted(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_types: tuple[str, ...],
) -> tuple[bool, int]:
    row = connection.execute(
        text(
            """
            SELECT COUNT(DISTINCT e.di_document_id) AS document_count
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
            "document_types": list(document_types),
        },
    ).mappings().one()
    count = int(row["document_count"] or 0)
    return count > 0, count


def _booking_receipt_total(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> Decimal:
    payments = connection.execute(
        text(
            """
            SELECT payment_id, amount, receipt_date, receipt_number
            FROM auditcore.payments
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND payment_stage='BOOKING'
              AND amount IS NOT NULL
              AND receipt_date IS NOT NULL
            ORDER BY receipt_date ASC, payment_id ASC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    receipt_records = [
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
    for group in compute_duplicate_groups(receipt_records):
        for duplicate in group.documents[1:]:
            excluded.add(duplicate.document_id)

    total = Decimal(0)
    for payment in payments:
        if payment["payment_id"] not in excluded:
            total += Decimal(str(payment["amount"]))
    return total


def _manual_verification_pending(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> int:
    legacy = connection.execute(
        text(
            """
            SELECT COUNT(*)
            FROM auditcore.workflow_tasks
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND task_type='MANUAL_VERIFICATION_REVIEW'
              AND task_status = ANY(:statuses)
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "statuses": list(_OPEN_LEGACY_MANUAL_TASKS),
        },
    ).scalar_one()

    p2 = connection.execute(
        text(
            """
            SELECT COUNT(*)
            FROM auditcore.p2_tasks
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND task_type='MANUAL_VERIFICATION_REVIEW'
              AND task_status = ANY(:statuses)
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "statuses": list(_OPEN_P2_MANUAL_TASKS),
        },
    ).scalar_one()
    return int(legacy or 0) + int(p2 or 0)


def _upsert_gate(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    gate_key: str,
    passed: bool,
    details_json: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_stage_gate_state (
                tenant_id, journey_id, stage_code, gate_key,
                gate_status, details, evaluated_at_utc
            ) VALUES (
                :tenant_id, :journey_id, 'BOOKING', :gate_key,
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
            "gate_key": gate_key,
            "gate_status": "PASS" if passed else "WAITING",
            "details": details_json,
        },
    )


def recompute_booking_stage(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, Any]:
    import json

    gates: dict[str, dict[str, Any]] = {}
    for gate_key, document_types in _BOOKING_DOC_GATES.items():
        passed, count = _document_extracted(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_types=document_types,
        )
        gates[gate_key] = {"passed": passed, "documentCount": count}
        _upsert_gate(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            gate_key=gate_key,
            passed=passed,
            details_json=json.dumps({"documentCount": count}),
        )

    minimum = _minimum_booking_amount(connection, tenant_id=tenant_id)
    receipt_total = _booking_receipt_total(
        connection, tenant_id=tenant_id, journey_id=journey_id,
    )
    payment_passed = receipt_total >= minimum
    gates["MINIMUM_BOOKING_PAYMENT"] = {
        "passed": payment_passed,
        "receiptTotal": str(receipt_total),
        "minimumAmount": str(minimum),
    }
    _upsert_gate(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        gate_key="MINIMUM_BOOKING_PAYMENT",
        passed=payment_passed,
        details_json=json.dumps(
            {"receiptTotal": str(receipt_total), "minimumAmount": str(minimum)}
        ),
    )

    manual_pending = _manual_verification_pending(
        connection, tenant_id=tenant_id, journey_id=journey_id,
    )
    manual_passed = manual_pending == 0
    gates["NO_MANUAL_VERIFICATION_PENDING"] = {
        "passed": manual_passed,
        "pendingCount": manual_pending,
    }
    _upsert_gate(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        gate_key="NO_MANUAL_VERIFICATION_PENDING",
        passed=manual_passed,
        details_json=json.dumps({"pendingCount": manual_pending}),
    )

    complete = all(item["passed"] for item in gates.values())
    any_document_missing = any(
        not gates[key]["passed"] for key in _BOOKING_DOC_GATES
    )
    if complete:
        current_stage = "BOOKING_COMPLETE"
        booking_state = "COMPLETE"
    elif any_document_missing or not payment_passed:
        current_stage = "BOOKING_DOCUMENT_UPLOAD"
        booking_state = "IN_PROGRESS"
    else:
        current_stage = "BOOKING_VERIFY_DOCUMENTS"
        booking_state = "BLOCKED"

    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_journey_runtime (
                tenant_id, journey_id, current_stage,
                booking_minimum_amount, booking_receipt_total,
                booking_completion_state, manual_verification_pending_count
            ) VALUES (
                :tenant_id, :journey_id, :current_stage,
                :minimum, :receipt_total, :booking_state, :manual_pending
            )
            ON CONFLICT (tenant_id, journey_id)
            DO UPDATE SET current_stage=CASE
                            WHEN auditcore.p2_journey_runtime.current_stage LIKE 'DELIVERY_%'
                            THEN auditcore.p2_journey_runtime.current_stage
                            ELSE EXCLUDED.current_stage
                          END,
                          booking_minimum_amount=EXCLUDED.booking_minimum_amount,
                          booking_receipt_total=EXCLUDED.booking_receipt_total,
                          booking_completion_state=EXCLUDED.booking_completion_state,
                          manual_verification_pending_count=EXCLUDED.manual_verification_pending_count,
                          updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "current_stage": current_stage,
            "minimum": minimum,
            "receipt_total": receipt_total,
            "booking_state": booking_state,
            "manual_pending": manual_pending,
        },
    )
    return {
        "stage": current_stage,
        "bookingCompletionState": booking_state,
        "minimumBookingAmount": str(minimum),
        "bookingReceiptTotal": str(receipt_total),
        "manualVerificationPending": manual_pending,
        "gates": gates,
    }


def read_booking_stage(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, Any]:
    runtime = connection.execute(
        text(
            """
            SELECT current_stage, booking_completion_state,
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
            SELECT gate_key, gate_status, details, evaluated_at_utc
            FROM auditcore.p2_stage_gate_state
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            ORDER BY gate_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    gates = {
        str(row["gate_key"]): {
            "passed": str(row["gate_status"]) == "PASS",
            **dict(row["details"] or {}),
        }
        for row in gate_rows
    }

    for gate_key in (*_BOOKING_DOC_GATES.keys(), "MINIMUM_BOOKING_PAYMENT", "NO_MANUAL_VERIFICATION_PENDING"):
        gates.setdefault(gate_key, {"passed": False})

    if runtime is None:
        minimum = _minimum_booking_amount(connection, tenant_id=tenant_id)
        return {
            "stage": "BOOKING_DOCUMENT_UPLOAD",
            "bookingCompletionState": "IN_PROGRESS",
            "minimumBookingAmount": str(minimum),
            "bookingReceiptTotal": "0",
            "manualVerificationPending": 0,
            "factVersion": 0,
            "gates": gates,
        }

    return {
        "stage": str(runtime["current_stage"]),
        "bookingCompletionState": str(runtime["booking_completion_state"]),
        "minimumBookingAmount": (
            str(runtime["booking_minimum_amount"])
            if runtime["booking_minimum_amount"] is not None
            else str(_minimum_booking_amount(connection, tenant_id=tenant_id))
        ),
        "bookingReceiptTotal": str(runtime["booking_receipt_total"] or 0),
        "manualVerificationPending": int(runtime["manual_verification_pending_count"] or 0),
        "factVersion": int(runtime["fact_version"] or 0),
        "gates": gates,
    }
