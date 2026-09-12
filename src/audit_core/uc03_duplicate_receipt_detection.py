"""uc03_duplicate_receipt_detection.py — the same physical receipt uploaded
more than once must not be counted as more money paid.

Scoped to receipts of the *same* document type only: dealer_receipt (Booking)
compared against dealer_receipt, payment_receipt (Delivery) against
payment_receipt -- never mixed. A Booking advance receipt and a Delivery
balance receipt legitimately coexist for the same amount; that is not a
duplicate, it is two different real payments, and reconciliation (a separate
concern, see uc03_payment_reconciliation.py) sums each type on its own.

Both receipt schemas (verigence-di schemas/dealer_receipt.py,
schemas/payment_receipt.py) require DI to read a ``receipt_number`` off every
receipt -- a real voucher/receipt number repeating, on two documents of the
same type for the same amount, is about as strong a duplicate signal as
extracted data ever gets (dealers do not reuse receipt numbers). When no
receipt number was legible on either document, this falls back to an
amount+date match instead -- weaker (two genuinely separate payments of the
same amount on the same day happen, e.g. two round-number instalments), so
that basis is called out explicitly in the finding's payload for the TL who
adjudicates it.

``sync_duplicate_receipt_detection`` is the producer: idempotent, self-heals
(a correction that no longer matches resolves the finding), never raises.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_delivery_commands import _machine_flag

logger = logging.getLogger(__name__)

_FINDING_TYPE = "DUPLICATE_RECEIPT"
_RULE_PREFIX = "DUPLICATE_RECEIPT"
_SEVERITY = "HIGH"

# Same-type only, by design (see module docstring) -- these are compared
# within each type separately, never against each other.
_RECEIPT_DOCUMENT_TYPES = ("dealer_receipt", "payment_receipt")
_RECEIPT_NUMBER_FIELD = "receipt_number"
_AMOUNT_FIELD = "amount_paid"
_DATE_FIELD = "receipt_date"
_FIELD_KEYS = (_RECEIPT_NUMBER_FIELD, _AMOUNT_FIELD, _DATE_FIELD)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _normalize_receipt_number(value: Any) -> str | None:
    text_value = str(value or "").strip().upper()
    return text_value or None


def _normalize_date(value: Any) -> str | None:
    text_value = str(value or "").strip()
    return text_value[:10] or None


@dataclass(frozen=True)
class _ReceiptDocument:
    document_id: UUID
    stage_code: str
    document_type_key: str
    receipt_number: str | None
    amount: Decimal | None
    receipt_date: str | None


@dataclass(frozen=True)
class _DuplicateGroup:
    rule_key: str
    stage_code: str
    document_type_key: str
    match_basis: str
    amount: Decimal
    documents: tuple[_ReceiptDocument, ...] = field(default_factory=tuple)


def _receipt_documents(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[_ReceiptDocument]:
    """One row per receipt document with its latest receipt_number/amount_paid/
    receipt_date, read from durable storage only (never DI directly)."""
    rows = connection.execute(
        text(
            """
            WITH ranked AS (
                SELECT
                    f.di_document_id, f.stage_code,
                    f.source_document_type_key AS document_type_key,
                    f.field_key, f.effective_value,
                    ROW_NUMBER() OVER (
                        PARTITION BY f.di_document_id, f.field_key
                        ORDER BY f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
                    ) AS row_rank
                FROM auditcore.journey_document_extracted_fields f
                WHERE f.tenant_id = :tenant_id AND f.journey_id = :journey_id
                  AND f.source_document_type_key = ANY(:document_types)
                  AND f.field_key = ANY(:field_keys)
                  AND f.effective_value IS NOT NULL
            )
            SELECT di_document_id, stage_code, document_type_key, field_key, effective_value
            FROM ranked WHERE row_rank = 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_types": list(_RECEIPT_DOCUMENT_TYPES),
            "field_keys": list(_FIELD_KEYS),
        },
    ).mappings().all()

    by_document: dict[UUID, dict[str, Any]] = {}
    for row in rows:
        entry = by_document.setdefault(
            row["di_document_id"],
            {"stage_code": row["stage_code"], "document_type_key": row["document_type_key"]},
        )
        entry[row["field_key"]] = row["effective_value"]

    return [
        _ReceiptDocument(
            document_id=document_id,
            stage_code=entry["stage_code"],
            document_type_key=entry["document_type_key"],
            receipt_number=_normalize_receipt_number(entry.get(_RECEIPT_NUMBER_FIELD)),
            amount=_to_decimal(entry.get(_AMOUNT_FIELD)),
            receipt_date=_normalize_date(entry.get(_DATE_FIELD)),
        )
        for document_id, entry in by_document.items()
    ]


def _duplicate_groups(documents: list[_ReceiptDocument]) -> list[_DuplicateGroup]:
    groups: list[_DuplicateGroup] = []
    by_type: dict[str, list[_ReceiptDocument]] = {}
    for document in documents:
        by_type.setdefault(document.document_type_key, []).append(document)

    for document_type_key, type_documents in by_type.items():
        with_number = [d for d in type_documents if d.receipt_number and d.amount is not None]
        without_number = [d for d in type_documents if not d.receipt_number and d.amount is not None]

        by_number_amount: dict[tuple[str, Decimal], list[_ReceiptDocument]] = {}
        for document in with_number:
            by_number_amount.setdefault((document.receipt_number, document.amount), []).append(document)
        for (receipt_number, amount), members in by_number_amount.items():
            if len(members) < 2:
                continue
            groups.append(
                _DuplicateGroup(
                    rule_key=f"{_RULE_PREFIX}:{document_type_key}:{receipt_number}:{amount}",
                    stage_code=members[0].stage_code,
                    document_type_key=document_type_key,
                    match_basis="RECEIPT_NUMBER_AND_AMOUNT",
                    amount=amount,
                    documents=tuple(members),
                )
            )

        by_amount_date: dict[tuple[Decimal, str], list[_ReceiptDocument]] = {}
        for document in without_number:
            if document.receipt_date is None:
                continue
            by_amount_date.setdefault((document.amount, document.receipt_date), []).append(document)
        for (amount, receipt_date), members in by_amount_date.items():
            if len(members) < 2:
                continue
            groups.append(
                _DuplicateGroup(
                    rule_key=f"{_RULE_PREFIX}:{document_type_key}:NOREF:{amount}:{receipt_date}",
                    stage_code=members[0].stage_code,
                    document_type_key=document_type_key,
                    match_basis="AMOUNT_AND_DATE_NO_RECEIPT_NUMBER",
                    amount=amount,
                    documents=tuple(members),
                )
            )

    return groups


def _resolve_stale_duplicate_findings(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    current_rule_keys: set[str],
    correlation_id: str,
) -> int:
    from audit_core.uc03_manual_verification import _resolve_finding

    rows = connection.execute(
        text(
            """
            SELECT audit_finding_id, stage_code, rule_key
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND finding_type_code=:finding_type
              AND finding_status IN ('OPEN','ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "finding_type": _FINDING_TYPE},
    ).mappings().all()
    resolved = 0
    for row in rows:
        if row["rule_key"] in current_rule_keys:
            continue
        _resolve_finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=row["stage_code"],
            finding_id=row["audit_finding_id"],
            actor_id=None,
            correlation_id=correlation_id,
            note="No longer a duplicate after correction (amount, receipt number, or date changed).",
        )
        resolved += 1
    return resolved


def sync_duplicate_receipt_detection(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    """Raise DUPLICATE_RECEIPT for each group of same-type receipts that look
    like the same physical receipt uploaded more than once; resolve a group
    once a correction breaks the match. Idempotent, self-heals, never raises."""
    try:
        documents = _receipt_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
        groups = _duplicate_groups(documents)

        raised = 0
        for group in groups:
            document_ids = [str(d.document_id) for d in group.documents]
            _machine_flag(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code=group.stage_code,
                rule_key=group.rule_key,
                finding_type=_FINDING_TYPE,
                severity=_SEVERITY,
                title=f"Possible duplicate {group.document_type_key.replace('_', ' ')} (₹{group.amount})",
                description=(
                    f"{len(group.documents)} {group.document_type_key.replace('_', ' ')} documents "
                    f"on this Journey all show ₹{group.amount}"
                    + (
                        " and the same receipt number -- almost certainly the same physical "
                        "receipt uploaded more than once."
                        if group.match_basis == "RECEIPT_NUMBER_AND_AMOUNT"
                        else " and the same date, with no receipt number legible on either -- "
                        "check whether this is one receipt uploaded twice or two separate "
                        "payments before counting both toward what the customer paid."
                    )
                ),
                correlation_id=correlation_id,
                safe_payload={
                    "documentTypeKey": group.document_type_key,
                    "matchBasis": group.match_basis,
                    "amount": str(group.amount),
                    "diDocumentIds": document_ids,
                },
            )
            raised += 1

        resolved = _resolve_stale_duplicate_findings(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            current_rule_keys={g.rule_key for g in groups},
            correlation_id=correlation_id,
        )
        return {
            "raised": raised,
            "resolved": resolved,
            "groupCount": len(groups),
            "examined": len(documents),
        }
    except Exception:
        logger.warning("sync_duplicate_receipt_detection failed", exc_info=True)
        return {"error": True}


__all__ = ["sync_duplicate_receipt_detection"]
