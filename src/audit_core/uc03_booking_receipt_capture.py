from __future__ import annotations

"""uc03_booking_receipt_capture.py — write a reviewed Dealer Receipt field into
its Audit Core Payment/receipt_details home.

Phase 0 dead-code cleanup removed this module's other half: it used to also
monkeypatch uc03_booking_capture._decide_proposal/_proposals/_completion_summary
to teach the V1 extraction-proposal accept/correct flow about receipt fields.
That whole flow (journey_capture_proposals) is retired end to end -- its
accept/correct routes had no live frontend caller, and its only remaining row
writer was itself dead code (see uc03_booking_capture.py's removal note at the
same commit). _write_receipt_capture below is unrelated to that flow and stays
very much live: uc03_pc_booking_documents.py, uc03_pc_generic_review.py, and
uc03_pc_direct_review.py all call it directly as the shared implementation of
"a PC reviewed/corrected a receipt field, write it to the Payment row."
"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core import uc03_booking_capture
from audit_core.errors import AuditCoreError

_RECEIPT_CAPTURE_MAP: dict[str, str] = {
    "dealer_name": "RECEIPT_DEALER_NAME",
    "dealer_gstin": "RECEIPT_DEALER_GSTIN",
    "customer_name": "RECEIPT_CUSTOMER_NAME",
    "customer_phone": "RECEIPT_CUSTOMER_PHONE",
    "receipt_number": "RECEIPT_NUMBER",
    "receipt_date": "RECEIPT_DATE",
    "amount_paid": "RECEIPT_AMOUNT",
    "payment_mode": "RECEIPT_PAYMENT_MODE",
    "payment_reference_no": "RECEIPT_PAYMENT_REFERENCE",
    "payment_reference_date": "RECEIPT_PAYMENT_REFERENCE_DATE",
    "bank_name": "RECEIPT_BANK_NAME",
    "bank_location": "RECEIPT_BANK_LOCATION",
    "booking_reference_number": "RECEIPT_BOOKING_REFERENCE",
    "remarks": "RECEIPT_REMARKS",
    "amount_in_words": "RECEIPT_AMOUNT_IN_WORDS",
}
_RECEIPT_DETAIL_KEYS: dict[str, str] = {
    "RECEIPT_DEALER_NAME": "dealer_name",
    "RECEIPT_DEALER_GSTIN": "dealer_gstin",
    "RECEIPT_CUSTOMER_NAME": "customer_name",
    "RECEIPT_CUSTOMER_PHONE": "customer_phone",
    "RECEIPT_PAYMENT_REFERENCE_DATE": "payment_reference_date",
    "RECEIPT_BANK_NAME": "bank_name",
    "RECEIPT_BANK_LOCATION": "bank_location",
    "RECEIPT_BOOKING_REFERENCE": "booking_reference_number",
    "RECEIPT_REMARKS": "remarks",
    "RECEIPT_AMOUNT_IN_WORDS": "amount_in_words",
}

def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _write_receipt_capture(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    capture_key: str,
    value: Any,
    source_evidence_id: UUID,
) -> tuple[str, str]:
    uc03_booking_capture._validate_evidence_for_journey(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        evidence_id=source_evidence_id,
    )

    row = connection.execute(
        text(
            """
            SELECT payment_id, receipt_details
            FROM auditcore.payments
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND source_evidence_id=:evidence_id
            ORDER BY created_at_utc, payment_id
            LIMIT 1
            FOR UPDATE
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evidence_id": source_evidence_id,
        },
    ).mappings().one_or_none()

    if row is None:
        payment_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.payments (
                    tenant_id, journey_id, amount, status_source,
                    source_evidence_id, receipt_details
                ) VALUES (
                    :tenant_id, :journey_id, 0, 'EVIDENCE',
                    :evidence_id, CAST(:details AS jsonb)
                )
                RETURNING payment_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "evidence_id": source_evidence_id,
                "details": json.dumps({"_part1AmountReviewed": False}),
            },
        ).scalar_one()
        details: dict[str, Any] = {"_part1AmountReviewed": False}
    else:
        payment_id = row["payment_id"]
        details = dict(row["receipt_details"] or {})

    params: dict[str, Any] = {
        "tenant_id": tenant_id,
        "payment_id": payment_id,
    }
    assignment: str

    if capture_key == "RECEIPT_NUMBER":
        assignment = "receipt_number=:value"
        params["value"] = _text_or_none(value)
    elif capture_key == "RECEIPT_DATE":
        assignment = "receipt_date=:value"
        params["value"] = (
            uc03_booking_capture._as_date(value, capture_key) if value is not None else None
        )
    elif capture_key == "RECEIPT_AMOUNT":
        if value is None:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Business validation failed",
                detail="Receipt Amount cannot be approved without a value.",
            )
        assignment = "amount=:value"
        params["value"] = uc03_booking_capture._as_decimal(value, capture_key)
        details["_part1AmountReviewed"] = True
    elif capture_key == "RECEIPT_PAYMENT_MODE":
        assignment = "payment_method_code=:value"
        params["value"] = _text_or_none(value)
    elif capture_key == "RECEIPT_PAYMENT_REFERENCE":
        assignment = "payment_reference=:value"
        params["value"] = _text_or_none(value)
    elif capture_key in _RECEIPT_DETAIL_KEYS:
        details[_RECEIPT_DETAIL_KEYS[capture_key]] = value
        assignment = "receipt_details=CAST(:details AS jsonb)"
        params["details"] = json.dumps(details, default=str)
    else:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Unsupported receipt field",
            detail="This Dealer Receipt field is not configured for review.",
        )

    # Keep the review metadata while updating a core receipt column as well.
    if capture_key not in _RECEIPT_DETAIL_KEYS:
        params["details"] = json.dumps(details, default=str)
        assignment = f"{assignment}, receipt_details=CAST(:details AS jsonb)"

    connection.execute(
        text(
            f"""
            UPDATE auditcore.payments
            SET {assignment},
                status_source='EVIDENCE',
                source_evidence_id=(
                    SELECT source_evidence_id
                    FROM auditcore.payments
                    WHERE tenant_id=:tenant_id AND payment_id=:payment_id
                ),
                updated_at_utc=now(),
                version_no=version_no+1
            WHERE tenant_id=:tenant_id AND payment_id=:payment_id
            """
        ),
        params,
    )
    return "PAYMENT", str(payment_id)


