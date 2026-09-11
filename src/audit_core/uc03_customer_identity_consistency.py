"""uc03_customer_identity_consistency.py — every document on a Journey must
belong to the same customer, and every receipt must belong to this dealer.

Once a KYC document (Aadhaar / PAN / a generic Customer KYC evidence) is
extracted, it establishes the customer's name for this Journey. Every other
document that also carries a person's name (Booking Form, Insurance Cover, a
Bank Approval Letter, any tax/customer invoice) -- uploaded before or after
that KYC document, order does not matter -- is checked against it. A
mismatch raises one HIGH-severity ``WRONG_DOCUMENT`` finding per mismatching
document: the wrong customer's paperwork may have been attached to this
Journey. This is a VIOLATION (TL adjudicates: Accept confirms the wrong
document was attached, Reject records a legitimate name variant this check's
fuzzy match under-scored), not a PC self-serve data gap.

Independently, every receipt (dealer_receipt / payment_receipt -- both
schemas require DI to read a dealer_name off the document) is checked
against this Journey's own dealer, from the master record
(auditcore.dealers), not another document. A mismatch means a receipt from a
different dealership was attached here -- same finding type, same severity,
its own rule_key so it tracks and self-heals independently of the
customer-name check on the same document.

``sync_customer_identity_consistency`` is the producer: idempotent, self-heals
(a later correction that now matches resolves the finding), never raises.
Runs alongside every other per-document sync producer, both stages.
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_delivery_commands import _machine_flag
from audit_core.uc03_manual_verification import _resolve_finding
from audit_core.uc03_v2_review_materialization import _INVOICE_DOCUMENT_TYPES

logger = logging.getLogger(__name__)

_FINDING_TYPE = "WRONG_DOCUMENT"
_RULE_PREFIX = "WRONG_DOCUMENT"
_DEALER_RULE_PREFIX = "WRONG_DOCUMENT:DEALER"
_SEVERITY = "HIGH"

# Both schemas (verigence-di schemas/dealer_receipt.py, payment_receipt.py)
# require DI to read dealer_name off every receipt.
_RECEIPT_DOCUMENT_TYPES: tuple[str, ...] = ("dealer_receipt", "payment_receipt")
_DEALER_NAME_FIELD = "dealer_name"

# KYC document types, in preference order when more than one is present --
# a government photo ID outranks a self-declared KYC form.
_KYC_DOCUMENT_TYPES: tuple[str, ...] = ("aadhaar", "pan_card", "customer_kyc")

# Every document type this check knows carries a natural-person name, and
# which field holds it. Invoice types all share the same DI schema (see
# uc03_v2_review_materialization._INVOICE_DOCUMENT_TYPES) so they all read
# buyer_name.
_NAME_FIELDS_BY_DOCUMENT_TYPE: dict[str, str] = {
    "aadhaar": "aadhaar_name",
    "pan_card": "pan_name",
    "customer_kyc": "customer_name",
    "booking_form": "customer_name",
    "insurance_cover": "insured_name",
    "bank_approval_letter": "applicant_name",
    # Both receipt schemas also require a customer_name -- checked here
    # alongside every other named document; their dealer_name is a
    # separate, independent check (see _receipt_dealer_names) against the
    # dealer master record, not another document's name.
    "dealer_receipt": "customer_name",
    "payment_receipt": "customer_name",
    **{document_type: "buyer_name" for document_type in _INVOICE_DOCUMENT_TYPES},
}

# Below this ratio on the normalized strings, two names are treated as
# genuinely different rather than a formatting/spelling variant. Deliberately
# tolerant, not exact-match: real names vary in spacing, initials, and minor
# transliteration/typos across independently-scanned documents. Biased toward
# flagging when uncertain -- a false positive costs a TL one Accept/Reject
# glance; a missed genuine mismatch costs a wrong customer's paperwork going
# unnoticed.
_NAME_MATCH_THRESHOLD = 0.55

_TITLE_PREFIX = re.compile(
    r"^(mr|mrs|ms|miss|dr|shri|smt|md|prof)\.?\s+", re.IGNORECASE
)
_NON_ALPHA = re.compile(r"[^A-Za-z\s]")
_WHITESPACE = re.compile(r"\s+")


def _normalize_name(value: Any) -> str:
    text_value = str(value or "").strip()
    text_value = _TITLE_PREFIX.sub("", text_value)
    text_value = _NON_ALPHA.sub(" ", text_value)
    return _WHITESPACE.sub(" ", text_value).strip().upper()


def _names_match(a: Any, b: Any) -> bool:
    """True when two extracted name strings plausibly refer to the same
    person. Empty/unparseable input is not this check's job to flag."""
    normalized_a, normalized_b = _normalize_name(a), _normalize_name(b)
    if not normalized_a or not normalized_b:
        return True
    if normalized_a == normalized_b:
        return True
    return SequenceMatcher(None, normalized_a, normalized_b).ratio() >= _NAME_MATCH_THRESHOLD


def _named_documents(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """One row per document that carries a mapped name field: its highest-
    confidence, most-recent value. Reads durable storage only, never DI."""
    mapping_values = ", ".join(
        f"(:doc_type_{i}, :field_key_{i})"
        for i in range(len(_NAME_FIELDS_BY_DOCUMENT_TYPE))
    )
    mapping_params = {
        f"{name}_{i}": value
        for i, (document_type, field_key) in enumerate(_NAME_FIELDS_BY_DOCUMENT_TYPE.items())
        for name, value in (("doc_type", document_type), ("field_key", field_key))
    }
    rows = connection.execute(
        text(
            f"""
            WITH mapping(document_type_key, field_key) AS (VALUES {mapping_values}),
            ranked AS (
                SELECT
                    f.di_document_id,
                    f.stage_code,
                    f.source_document_type_key AS document_type_key,
                    f.effective_value,
                    ROW_NUMBER() OVER (
                        PARTITION BY f.di_document_id
                        ORDER BY f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
                    ) AS row_rank
                FROM auditcore.journey_document_extracted_fields f
                JOIN mapping m
                  ON m.document_type_key = f.source_document_type_key
                 AND m.field_key = f.field_key
                WHERE f.tenant_id = :tenant_id AND f.journey_id = :journey_id
                  AND f.effective_value IS NOT NULL
            )
            SELECT di_document_id, stage_code, document_type_key, effective_value
            FROM ranked WHERE row_rank = 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, **mapping_params},
    ).mappings().all()
    return [dict(row) for row in rows]


def _reference_name(documents: list[dict[str, Any]]) -> tuple[str, UUID] | None:
    """The customer's KYC name, and the document it came from, by preference
    order. None when no KYC document has been extracted yet -- nothing to
    check other documents against."""
    for kyc_type in _KYC_DOCUMENT_TYPES:
        for document in documents:
            if document["document_type_key"] == kyc_type:
                name = str(document["effective_value"] or "").strip()
                if name:
                    return name, document["di_document_id"]
    return None


def _journey_dealer_name(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> str | None:
    """This Journey's actual dealer, from the master record -- not another
    document. None if the journey/dealer row can't be resolved (never the
    normal case; the caller treats it as nothing to check against)."""
    return connection.execute(
        text(
            """
            SELECT d.dealer_name
            FROM auditcore.journeys j
            JOIN auditcore.dealers d
              ON d.tenant_id = j.tenant_id AND d.dealer_id = j.dealer_id
            WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()


def _receipt_dealer_names(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """One row per receipt document with its latest dealer_name value."""
    rows = connection.execute(
        text(
            """
            WITH ranked AS (
                SELECT
                    f.di_document_id, f.stage_code,
                    f.source_document_type_key AS document_type_key,
                    f.effective_value,
                    ROW_NUMBER() OVER (
                        PARTITION BY f.di_document_id
                        ORDER BY f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
                    ) AS row_rank
                FROM auditcore.journey_document_extracted_fields f
                WHERE f.tenant_id = :tenant_id AND f.journey_id = :journey_id
                  AND f.source_document_type_key = ANY(:document_types)
                  AND f.field_key = :field_key
                  AND f.effective_value IS NOT NULL
            )
            SELECT di_document_id, stage_code, document_type_key, effective_value
            FROM ranked WHERE row_rank = 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_types": list(_RECEIPT_DOCUMENT_TYPES),
            "field_key": _DEALER_NAME_FIELD,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _resolve_if_open(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    rule_key: str,
    stage_code: str,
    correlation_id: str,
    note: str,
) -> bool:
    """Resolve an open finding for rule_key, if one exists. Returns whether
    a finding was actually resolved (for the caller's own counter)."""
    finding_id = connection.execute(
        text(
            """
            SELECT audit_finding_id FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND rule_key=:rule_key AND finding_status IN ('OPEN','ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "rule_key": rule_key},
    ).scalar_one_or_none()
    if finding_id is None:
        return False
    _resolve_finding(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
        finding_id=finding_id,
        actor_id=None,
        correlation_id=correlation_id,
        note=note,
    )
    return True


def sync_customer_identity_consistency(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    """Raise WRONG_DOCUMENT for any document whose name doesn't match the
    Journey's KYC name; resolve it once a later correction matches. Idempotent,
    self-heals on read, never raises."""
    try:
        raised = 0
        resolved = 0
        reference_name: str | None = None

        documents = _named_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
        reference = _reference_name(documents)
        # Unlike the dealer-name check below (checked against a master
        # record, always available), there is nothing to check customer
        # names against until a KYC document exists -- skip only this half,
        # not the whole producer, so the dealer-name check still runs.
        if reference is not None:
            reference_name, reference_document_id = reference
            for document in documents:
                document_id = document["di_document_id"]
                if document_id == reference_document_id:
                    continue
                document_name = str(document["effective_value"] or "").strip()
                rule_key = f"{_RULE_PREFIX}:{document_id}"
                if not document_name or _names_match(reference_name, document_name):
                    if _resolve_if_open(
                        connection,
                        tenant_id=tenant_id,
                        journey_id=journey_id,
                        rule_key=rule_key,
                        stage_code=document["stage_code"],
                        correlation_id=correlation_id,
                        note="Name now matches the customer's KYC document.",
                    ):
                        resolved += 1
                    continue

                _machine_flag(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    stage_code=document["stage_code"],
                    rule_key=rule_key,
                    finding_type=_FINDING_TYPE,
                    severity=_SEVERITY,
                    title=f"Document name does not match the customer's KYC ({document['document_type_key']})",
                    description=(
                        f"This {document['document_type_key']} shows the name "
                        f"'{document_name}', which does not match the customer's "
                        f"KYC name '{reference_name}' on this Journey. Confirm "
                        "whether the wrong customer's document was attached."
                    ),
                    correlation_id=correlation_id,
                    safe_payload={
                        "documentTypeKey": document["document_type_key"],
                        "diDocumentId": str(document_id),
                        "extractedName": document_name,
                        "kycName": reference_name,
                        "kycDocumentId": str(reference_document_id),
                    },
                )
                raised += 1

        dealer_name = _journey_dealer_name(connection, tenant_id=tenant_id, journey_id=journey_id)
        if dealer_name:
            for receipt in _receipt_dealer_names(connection, tenant_id=tenant_id, journey_id=journey_id):
                document_id = receipt["di_document_id"]
                receipt_dealer_name = str(receipt["effective_value"] or "").strip()
                rule_key = f"{_DEALER_RULE_PREFIX}:{document_id}"
                if not receipt_dealer_name or _names_match(dealer_name, receipt_dealer_name):
                    if _resolve_if_open(
                        connection,
                        tenant_id=tenant_id,
                        journey_id=journey_id,
                        rule_key=rule_key,
                        stage_code=receipt["stage_code"],
                        correlation_id=correlation_id,
                        note="Dealer name now matches this Journey's dealer.",
                    ):
                        resolved += 1
                    continue

                _machine_flag(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    stage_code=receipt["stage_code"],
                    rule_key=rule_key,
                    finding_type=_FINDING_TYPE,
                    severity=_SEVERITY,
                    title=f"Receipt dealer name does not match this Journey's dealer ({receipt['document_type_key']})",
                    description=(
                        f"This {receipt['document_type_key']} shows dealer "
                        f"'{receipt_dealer_name}', which does not match this "
                        f"Journey's dealer '{dealer_name}'. Confirm whether a "
                        "receipt from a different dealership was attached here."
                    ),
                    correlation_id=correlation_id,
                    safe_payload={
                        "documentTypeKey": receipt["document_type_key"],
                        "diDocumentId": str(document_id),
                        "extractedDealerName": receipt_dealer_name,
                        "journeyDealerName": dealer_name,
                    },
                )
                raised += 1

        return {"raised": raised, "resolved": resolved, "referenceName": reference_name}
    except Exception:
        logger.warning("sync_customer_identity_consistency failed", exc_info=True)
        return {"error": True}


__all__ = ["sync_customer_identity_consistency"]
