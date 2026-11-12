"""A Vehicle Delivery Receipt that carries a delivery date is the gate pass
(product owner 2026-11-12).

"Can't we put an additional rule that if there is delivery date mentioned,
let's consider that as gate pass."

The delivery date of a booking, the day's gate pass count, the exports and the
delivery checks all read the document typed ``gate_pass``. Document
Intelligence types some dealers' gate pass as ``delivery_receipt`` (Vehicle
Delivery Receipt): the form the customer signs for the vehicle received. Live
on Mahindra Orissa, 3 to 11 Nov: 64 of Utkal's 156 gate passes, then three of
Shivnath's on 11 Nov -- each delivery left without a delivery date and its
gate pass still "Missing". The classifier's definitions were sharpened
(verigence-di 0064); this rule holds whatever it decides.

Before a booking is rebuilt from its documents, a Vehicle Delivery Receipt
with a delivery date read on it, on a booking with no gate pass, is counted
as the gate pass: its document, its page and its read values are typed
``gate_pass``. Nothing is read again, so a date the PC corrected on it stays.
Every reader of the gate pass then sees it; none had to learn a second type.

Left as they are: a receipt with no delivery date read, a booking that
already has a gate pass (the receipt stays beside it), and a page whose type
a person set by hand.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

RECEIPT = "delivery_receipt"
GATE_PASS = "gate_pass"
DATE_FIELD = "delivery_date"
ACTOR = "SYSTEM:DELIVERY_RECEIPT_GATE_PASS"


def _set_by_a_person(actor: Any) -> bool:
    return bool(actor) and not str(actor).upper().startswith("SYSTEM")


def count_delivery_receipt_as_gate_pass(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str | None = None,
) -> list[dict[str, Any]]:
    """Type the booking's Vehicle Delivery Receipt as its gate pass when it
    carries a delivery date and the booking has no gate pass. Returns what was
    typed. Idempotent: once typed, the booking has a gate pass."""
    from audit_core.uc03_p2_runtime import record_activity

    has_gate_pass = connection.execute(
        text(
            "SELECT 1 FROM auditcore.evidence WHERE tenant_id=:t AND journey_id=:j "
            "AND association_status='ACTIVE' AND document_type_key=:k LIMIT 1"
        ),
        {"t": tenant_id, "j": journey_id, "k": GATE_PASS},
    ).first()
    if has_gate_pass is not None:
        return []
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (e.di_document_id)
                   e.di_document_id, f.effective_value #>> '{}' AS delivery_date,
                   q.queue_id, q.type_overridden_by_actor_id
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id = e.tenant_id AND f.journey_id = e.journey_id
             AND f.di_document_id = e.di_document_id AND f.field_key = :field
            LEFT JOIN auditcore.p2_document_queue q
              ON q.tenant_id = e.tenant_id AND q.journey_id = e.journey_id
             AND q.di_document_id = e.di_document_id AND q.retired_at_utc IS NULL
             AND q.queue_status NOT IN ('MERGED', 'CANCELLED')
            WHERE e.tenant_id = :t AND e.journey_id = :j AND e.association_status = 'ACTIVE'
              AND e.document_type_key = :receipt
              AND NULLIF(btrim(COALESCE(f.effective_value #>> '{}', '')), '') IS NOT NULL
            ORDER BY e.di_document_id, e.linked_at_utc DESC
            """
        ),
        {"t": tenant_id, "j": journey_id, "field": DATE_FIELD, "receipt": RECEIPT},
    ).mappings().all()
    candidates = [row for row in rows if not _set_by_a_person(row["type_overridden_by_actor_id"])]
    if not candidates:
        return []
    # One gate pass per booking: with two receipts on file, the first is it and
    # the other stays a receipt beside it.
    row = candidates[0]
    params = {"t": tenant_id, "j": journey_id, "d": row["di_document_id"], "k": GATE_PASS, "receipt": RECEIPT}
    connection.execute(
        text(
            "UPDATE auditcore.evidence SET document_type_key=:k "
            "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d AND association_status='ACTIVE' "
            "AND document_type_key=:receipt"
        ),
        params,
    )
    connection.execute(
        text(
            "UPDATE auditcore.journey_document_extracted_fields SET source_document_type_key=:k "
            "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d AND source_document_type_key=:receipt"
        ),
        params,
    )
    if row["queue_id"] is not None:
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_document_queue
                SET template_key = :k, type_overridden_by_actor_id = :actor,
                    status_reason = :why, updated_at_utc = now()
                WHERE tenant_id = :t AND queue_id = :q
                """
            ),
            {"t": tenant_id, "q": row["queue_id"], "k": GATE_PASS, "actor": ACTOR,
             "why": "Counted as the gate pass: a Vehicle Delivery Receipt carrying the delivery date "
                    f"({row['delivery_date']})."},
        )
    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="PAGE_RETYPED",
        subject_type="DOCUMENT_PAGE", subject_id=str(row["queue_id"] or row["di_document_id"]),
        details={"templateKey": GATE_PASS, "was": RECEIPT, "by": "system:delivery-receipt-gate-pass",
                 "deliveryDate": row["delivery_date"]},
        correlation_id=correlation_id,
    )
    return [{"diDocumentId": str(row["di_document_id"]), "deliveryDate": row["delivery_date"]}]
