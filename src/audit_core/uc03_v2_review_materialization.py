from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core import uc03_booking_capture
from audit_core.uc03_booking_receipt_capture import (
    _RECEIPT_CAPTURE_MAP,
    _RECEIPT_DETAIL_KEYS,
    _RECEIPT_DOCUMENT_TYPE,
)

logger = logging.getLogger(__name__)


def receipt_review_key(receipt_ordinal: int, field_key: str) -> str:
    """Return the readable/stable review key for one field on one receipt."""

    return f"raw:receipt_{receipt_ordinal}_{field_key.strip().lower()}"


def receipt_document_ordinals(document_ids: list[UUID]) -> dict[UUID, int]:
    """Assign stable ordinals after capture is closed, without storing another ID."""

    ordered = sorted(set(document_ids), key=str)
    return {document_id: index + 1 for index, document_id in enumerate(ordered)}


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized or None


def _reviewed_receipt_values(
    document: Any,
    *,
    receipt_ordinal: int,
    rejected_review_keys: set[str],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    details: dict[str, Any] = {}

    for field in document.fields:
        source_field_key = str(field.fieldKey).strip().lower()
        capture_key = _RECEIPT_CAPTURE_MAP.get(source_field_key)
        if capture_key is None:
            continue
        if receipt_review_key(receipt_ordinal, source_field_key) in rejected_review_keys:
            continue
        value = field.value
        if value is None or value == "":
            continue

        if capture_key == "RECEIPT_NUMBER":
            values["receipt_number"] = _text_or_none(value)
        elif capture_key == "RECEIPT_DATE":
            values["receipt_date"] = uc03_booking_capture._as_date(value, capture_key)
        elif capture_key == "RECEIPT_AMOUNT":
            values["amount"] = uc03_booking_capture._as_decimal(value, capture_key)
        elif capture_key == "RECEIPT_PAYMENT_MODE":
            values["payment_method_code"] = _text_or_none(value)
        elif capture_key == "RECEIPT_PAYMENT_REFERENCE":
            values["payment_reference"] = _text_or_none(value)
        elif capture_key in _RECEIPT_DETAIL_KEYS:
            details[_RECEIPT_DETAIL_KEYS[capture_key]] = value

    values["receipt_details"] = details
    return values


def _existing_payment(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    evidence_id: UUID | None,
):
    row = connection.execute(
        text(
            """
            SELECT payment_id, source_evidence_id, source_di_document_id,
                   receipt_number, receipt_date, amount, payment_method_code,
                   payment_reference, receipt_details, payment_stage, status_source
            FROM auditcore.payments
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND source_di_document_id=:document_id
            ORDER BY created_at_utc, payment_id
            LIMIT 1
            FOR UPDATE
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
        },
    ).mappings().one_or_none()
    if row is not None or evidence_id is None:
        return row

    # Backward-compatible reuse when a legacy Evidence-backed Payment already
    # exists for the same receipt. This prevents a V2 review from duplicating it.
    return connection.execute(
        text(
            """
            SELECT payment_id, source_evidence_id, source_di_document_id,
                   receipt_number, receipt_date, amount, payment_method_code,
                   payment_reference, receipt_details, payment_stage, status_source
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
            "evidence_id": evidence_id,
        },
    ).mappings().one_or_none()


def _same_payment_state(
    row: Any,
    *,
    document_id: UUID,
    evidence_id: UUID | None,
    values: dict[str, Any],
) -> bool:
    return (
        row["source_di_document_id"] == document_id
        and row["source_evidence_id"] == evidence_id
        and row["receipt_number"] == values.get("receipt_number")
        and row["receipt_date"] == values.get("receipt_date")
        and row["amount"] == values["amount"]
        and row["payment_method_code"] == values.get("payment_method_code")
        and row["payment_reference"] == values.get("payment_reference")
        and dict(row["receipt_details"] or {}) == values["receipt_details"]
        and row["payment_stage"] == "BOOKING"
        and row["status_source"] == "EVIDENCE"
    )


def materialize_reviewed_booking_receipts(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
    rejected_review_keys: set[str],
) -> dict[str, int]:
    """Persist each reviewed Dealer Receipt with at most one row write.

    All receipt fields are collected in memory first. A receipt without an accepted
    amount is not turned into a zero-value Payment; the later Booking audit can flag
    the missing/invalid amount without contaminating cumulative-payment logic.
    """

    receipt_documents = [
        document
        for document in documents
        if str(document.documentTypeKey or "").strip().lower() == _RECEIPT_DOCUMENT_TYPE
        and str(document.extractionState).upper() == "READY"
    ]
    ordinals = receipt_document_ordinals(
        [document.documentId for document in receipt_documents]
    )

    created = 0
    updated = 0
    unchanged = 0
    skipped_without_amount = 0

    for document in receipt_documents:
        values = _reviewed_receipt_values(
            document,
            receipt_ordinal=ordinals[document.documentId],
            rejected_review_keys=rejected_review_keys,
        )
        if "amount" not in values:
            skipped_without_amount += 1
            continue

        evidence_id = document.evidenceId
        existing = _existing_payment(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document.documentId,
            evidence_id=evidence_id,
        )

        params = {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document.documentId,
            "evidence_id": evidence_id,
            "receipt_number": values.get("receipt_number"),
            "receipt_date": values.get("receipt_date"),
            "amount": values["amount"],
            "payment_method_code": values.get("payment_method_code"),
            "payment_reference": values.get("payment_reference"),
            "receipt_details": json.dumps(values["receipt_details"], default=str),
        }

        if existing is None:
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.payments (
                        tenant_id, journey_id, amount, payment_method_code,
                        payment_reference, status_source, source_evidence_id,
                        source_di_document_id, receipt_number, receipt_date,
                        receipt_details, payment_stage
                    ) VALUES (
                        :tenant_id, :journey_id, :amount, :payment_method_code,
                        :payment_reference, 'EVIDENCE', :evidence_id,
                        :document_id, :receipt_number, :receipt_date,
                        CAST(:receipt_details AS jsonb), 'BOOKING'
                    )
                    """
                ),
                params,
            )
            created += 1
            continue

        if _same_payment_state(
            existing,
            document_id=document.documentId,
            evidence_id=evidence_id,
            values=values,
        ):
            unchanged += 1
            continue

        connection.execute(
            text(
                """
                UPDATE auditcore.payments
                SET amount=:amount,
                    payment_method_code=:payment_method_code,
                    payment_reference=:payment_reference,
                    status_source='EVIDENCE',
                    source_evidence_id=:evidence_id,
                    source_di_document_id=:document_id,
                    receipt_number=:receipt_number,
                    receipt_date=:receipt_date,
                    receipt_details=CAST(:receipt_details AS jsonb),
                    payment_stage='BOOKING',
                    updated_at_utc=now(),
                    version_no=version_no+1
                WHERE tenant_id=:tenant_id
                  AND payment_id=:payment_id
                """
            ),
            {**params, "payment_id": existing["payment_id"]},
        )
        updated += 1

    result = {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "skippedWithoutAmount": skipped_without_amount,
    }
    logger.info(
        "UC03 V2 reviewed receipts materialized",
        extra={
            "tenant_id": tenant_id,
            "journey_id": str(journey_id),
            "created_count": created,
            "updated_count": updated,
            "unchanged_count": unchanged,
            "skipped_without_amount_count": skipped_without_amount,
        },
    )
    return result
