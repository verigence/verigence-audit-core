"""uc03_deal_source_policy.py -- which document a Project's Deal takes its billed
figures from.

Every Project reads its Deal from the customer's invoices (``INVOICE``), as it
always has. Mahindra Orissa (product owner, 2026-10-25) takes them from the
dealer's account statement instead (``ACCOUNT_STATEMENT``): the handwritten
settlement form each dealer fills in at delivery, stored as ``customer_ledger``.
The policy is one column on ``auditcore.projects`` (migration 0162), so it is a
setting a Project has, not a tenant id written into the code.

This module is deliberately free of imports from the rest of Audit Core so the
Deal writers, the Deal view and the account-statement reader can all ask the
same question without importing each other.
"""
from __future__ import annotations

import re

from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

POLICY_INVOICE = "INVOICE"
POLICY_ACCOUNT_STATEMENT = "ACCOUNT_STATEMENT"

# Every document type that bills a customer (or adjusts what was billed) and so
# writes the Deal's "billed" figures. The insurance cover note and the RTO
# challan are not invoices and keep their own place in the Deal.
INVOICE_SOURCE_TYPES = frozenset({
    "tax_invoice_tally",
    "customer_invoice_dms",
    "customer_invoice_dms_v2",
    "invoice_generic",
    "tax_invoice",
    "tax_invoice_dms",
    "wholesale_invoice",
    "accessory_invoice_dms",
    "accessory_invoice_tally",
    "ew_invoice",
    "rsa_invoice",
    "credit_note",
    "debit_note",
})

# The account statement is filed by DI as a customer ledger.
ACCOUNT_STATEMENT_TYPE = "customer_ledger"


def normalise_type(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def is_invoice_type(document_type: object) -> bool:
    return normalise_type(document_type) in INVOICE_SOURCE_TYPES


def deal_source_policy(connection: Connection, *, tenant_id: str) -> str:
    """The Project's policy. Anything unreadable -- the column not there yet
    during a deploy, no Project row -- is the default ``INVOICE``: the
    behaviour every other Project has."""
    try:
        with connection.begin_nested():
            value = connection.execute(
                text("SELECT deal_source_policy FROM auditcore.projects WHERE tenant_id = :t"),
                {"t": tenant_id},
            ).scalar_one_or_none()
    except SQLAlchemyError:
        return POLICY_INVOICE
    return POLICY_ACCOUNT_STATEMENT if value == POLICY_ACCOUNT_STATEMENT else POLICY_INVOICE


def account_statement_is_source_of_truth(connection: Connection, *, tenant_id: str) -> bool:
    return deal_source_policy(connection, tenant_id=tenant_id) == POLICY_ACCOUNT_STATEMENT


def invoice_withheld(connection: Connection, *, tenant_id: str, document_type: object) -> bool:
    """True when this Project's Deal must not take a value from this document:
    it is an invoice and the account statement is the source of truth."""
    return is_invoice_type(document_type) and account_statement_is_source_of_truth(
        connection, tenant_id=tenant_id)


__all__ = [
    "ACCOUNT_STATEMENT_TYPE",
    "INVOICE_SOURCE_TYPES",
    "POLICY_ACCOUNT_STATEMENT",
    "POLICY_INVOICE",
    "account_statement_is_source_of_truth",
    "deal_source_policy",
    "invoice_withheld",
    "is_invoice_type",
    "normalise_type",
]
