from __future__ import annotations

"""uc03_document_registry.py — single source of truth for UC03 document-type
metadata shared across capture, sync, review and materialization.

Phase 0 of the UC03 document-pipeline redesign (see the approved redesign plan).
Booking and Delivery each register their own receipt requirement under a
different ``document_type_key`` — Booking's default profile uses
``dealer_receipt``; Delivery's (migrations 0017/0022) uses ``payment_receipt``.
Both are, in every functional sense, the same kind of document: evidence of a
payment that should get per-instance review grouping and drive payment
reconciliation. Before this module existed, "is this document a receipt" was
answered by an independently hand-copied ``_RECEIPT_DOCUMENT_TYPE = "dealer_receipt"``
constant in five different files. Four of them only ever recognized
``dealer_receipt`` and silently mistreated a Delivery ``payment_receipt``
document as an ordinary field — this is the same class of bug already found and
fixed once this session in ``uc03_delivery_review_materialization.py``
(``_RECEIPT_DOCUMENT_TYPES``) and ``uc03_confidence_review_policy.py``
(``document_type_key in (...)``); this module makes that fix the *only* copy of
the truth, going forward.

Every current and future receipt/bank-statement/reconciliation-trigger check
should import from here rather than redefine the type list locally.
"""

RECEIPT_DOCUMENT_TYPES = frozenset({"dealer_receipt", "payment_receipt"})
"""Every document_type_key that represents a payment receipt, across both stages."""

BANK_STATEMENT_DOCUMENT_TYPE = "bank_statement_extract"
"""The single document_type_key DI emits for a reviewed bank statement
(verigence-di ``schemas/bank_statement.py``; registered as a standing, non-
checklist document type via migration 0070 for both Booking and Delivery)."""

RECONCILIATION_TRIGGER_DOCUMENT_TYPES = RECEIPT_DOCUMENT_TYPES | {
    BANK_STATEMENT_DOCUMENT_TYPE
}
"""Document types whose confirmation should trigger payment reconciliation
(see ``uc03_payment_reconciliation.py`` / ``uc03_confidence_review_policy.py``)."""


def is_receipt_document_type(document_type_key: str | None) -> bool:
    """True for a payment receipt under either stage's document_type_key."""
    return _normalize(document_type_key) in RECEIPT_DOCUMENT_TYPES


def is_bank_statement_document_type(document_type_key: str | None) -> bool:
    """True for a reviewed bank statement extract."""
    return _normalize(document_type_key) == BANK_STATEMENT_DOCUMENT_TYPE


def is_reconciliation_trigger_document_type(document_type_key: str | None) -> bool:
    """True if confirming this document should (re-)run payment reconciliation."""
    return _normalize(document_type_key) in RECONCILIATION_TRIGGER_DOCUMENT_TYPES


def _normalize(document_type_key: str | None) -> str:
    return str(document_type_key or "").strip().lower()
