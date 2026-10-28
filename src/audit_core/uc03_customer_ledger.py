"""NDC (account statement) or customer ledger (product owner, 2026-10-28).

DI files two different documents under one type, ``customer_ledger``:

* the dealer's account statement -- this Project's NDC: every charge of the
  deal (vehicle cost, TCS, insurance, registration, accessories ...), the
  discounts given and the payments. It fills the Deal (uc03_account_statement).
* the customer ledger -- a Tally or ERP printout of the customer's account:
  voucher postings with a running balance (Tally) or a TOTAL / NET VEHICLE
  COST line (ERP). It talks about the payments only. It is not the NDC; a
  Tally ledger checks the NDC's vehicle, finance and insurance postings.

"Account statement is different from customer ledger in a way customer ledger
only talks about payment but account statement talks about all the details."
A ledger was taken as the NDC: the checklist showed the required NDC received
on 51 bookings that had only a ledger.

The two are told apart by what was read, with the same test the Deal uses
(uc03_account_statement.statement_kind): a running balance on any line is a
Tally ledger; a TOTAL / NET VEHICLE COST line is an ERP ledger. Anything else
-- the statement form, or a page nothing could be read from -- stays the NDC.
``payment_ledger_sql`` is that test in SQL, for the checklist counts; a test
keeps the two in step.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

LEDGER_DI_TYPE = "customer_ledger"
NDC_TEMPLATE = "customer_ledger"
PAYMENT_LEDGER_TEMPLATE = "customer_payment_ledger"
PAYMENT_LEDGER_NAME = "Customer Ledger"


def is_payment_ledger(line_items: Any) -> bool:
    """Whether a customer_ledger document's line items are a Tally or ERP
    ledger rather than the account statement."""
    from audit_core.uc03_account_statement import (
        KIND_ERP,
        KIND_TALLY,
        line_rows,
        statement_kind,
    )

    rows = line_rows(line_items)
    return bool(rows) and statement_kind(rows) in (KIND_TALLY, KIND_ERP)


def _rows_sql(value: str) -> str:
    """The line items as a jsonb array, whether stored as an array or as the
    JSON text of one; anything else is no lines."""
    inner = f"({value} #>> '{{}}')"
    return f"""(CASE
        WHEN jsonb_typeof({value}) = 'array' THEN {value}
        WHEN jsonb_typeof({value}) = 'string' THEN
          CASE WHEN pg_input_is_valid({inner}, 'jsonb') THEN
            CASE WHEN jsonb_typeof({inner}::jsonb) = 'array' THEN {inner}::jsonb ELSE '[]'::jsonb END
          ELSE '[]'::jsonb END
        ELSE '[]'::jsonb END)"""


def payment_ledger_sql(evidence: str = "e") -> str:
    """SQL: true when the evidence row ``evidence`` is a customer ledger, not
    the account statement (``is_payment_ledger`` in SQL)."""
    e = evidence
    label = ("btrim(regexp_replace(regexp_replace(upper(COALESCE(lrow ->> 'particulars_raw', '')), "
             "'\\*+', '', 'g'), '\\s+', ' ', 'g'))")
    balance = "lrow -> 'running_balance'"
    return f"""(
      {e}.document_type_key = '{LEDGER_DI_TYPE}' AND EXISTS (
        SELECT 1 FROM auditcore.journey_document_extracted_fields lf
        CROSS JOIN LATERAL jsonb_array_elements({_rows_sql('lf.effective_value')}) AS lrow
        WHERE lf.tenant_id = {e}.tenant_id AND lf.journey_id = {e}.journey_id
          AND lf.di_document_id = {e}.di_document_id AND lf.field_key = 'line_items'
          AND jsonb_typeof(lrow) = 'object'
          AND (
            (jsonb_typeof({balance}) = 'string' AND {balance} #>> '{{}}' <> '')
            OR (jsonb_typeof({balance}) = 'number' AND ({balance} #>> '{{}}')::numeric <> 0)
            OR (jsonb_typeof({balance}) = 'array' AND {balance} <> '[]'::jsonb)
            OR (jsonb_typeof({balance}) = 'object' AND {balance} <> '{{}}'::jsonb)
            OR (jsonb_typeof({balance}) = 'boolean' AND ({balance})::text = 'true')
            OR {label} ~ '^(TOTAL|NET) VEHICLE COST$'
          )
      )
    )"""


def payment_ledger_ids(connection: Connection, *, tenant_id: str, journey_id: UUID) -> set[str]:
    """The di_document_ids of the Journey's active documents that are a
    customer ledger, not the NDC."""
    return {str(d) for d in connection.execute(
        text(f"""
            SELECT e.di_document_id FROM auditcore.evidence e
            WHERE e.tenant_id = :t AND e.journey_id = :j AND e.association_status = 'ACTIVE'
              AND {payment_ledger_sql('e')}
        """),
        {"t": tenant_id, "j": journey_id},
    ).scalars().all()}


__all__ = [
    "LEDGER_DI_TYPE",
    "NDC_TEMPLATE",
    "PAYMENT_LEDGER_NAME",
    "PAYMENT_LEDGER_TEMPLATE",
    "is_payment_ledger",
    "payment_ledger_ids",
    "payment_ledger_sql",
]
