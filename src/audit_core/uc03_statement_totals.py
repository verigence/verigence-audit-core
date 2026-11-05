"""The NDC's and the cost sheet's rows add up to the totals they print
(product owner 2026-11-05: "NDC rows should add up and so does cost sheet").

A printed total is the dealer's own sum. When the rows read do not give it,
a row was misread, read twice or not read, and every figure the Deal takes
from that copy is in doubt. This module only measures: per copy on file, the
rows read added up beside each total the copy prints.

``journey_statement_totals`` is the "statement-totals" section of a journey
snapshot (uc03_journey_snapshot), read only.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

from audit_core.uc03_account_statement import (
    ACCOUNT_STATEMENT_TYPE,
    COST_SHEET_TYPE,
    _amount,
    line_rows,
    load_ledger_documents,
    read_statement,
)

# A total and its rows agree within this (the form's own rounding).
ADDS_UP_TOLERANCE = Decimal(10)

_STATEMENT_PRINTED = ("total_cost", "gross_value", "discount_value", "discount_details_total", "net_value",
                      "total_receipts", "balance_amount", "total_debited", "total_credited", "closing_balance")
_COST_SHEET_PRINTED = ("ex_showroom_price", "rto_amount", "insurance_amount", "accessories_amount",
                       "discount_amount", "total_on_road_price")


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal(1))) if value == value.to_integral() else str(value)


def statement_sums(fields: dict[str, Any]) -> dict[str, Any]:
    """The rows of one NDC copy added up, as read_statement reads them."""
    lines = line_rows(fields.get("line_items"))
    read = read_statement(lines)
    debits = sum((abs(a) for r in lines if (a := _amount(r.get("debit_amount"))) is not None), Decimal(0))
    credits = sum((abs(a) for r in lines if (a := _amount(r.get("credit_amount"))) is not None), Decimal(0))
    return {
        "kind": read.kind,
        "rows": len(lines),
        "charges": _money(sum(read.components.values(), Decimal(0))),
        "components": {k: _money(v) for k, v in sorted(read.components.items())},
        "discounts": _money(sum(read.discounts.values(), Decimal(0))),
        "oldVehicle": _money(read.old_vehicle_value),
        "receipts": _money(read.receipts),
        "received": _money(sum((a for _, a in read.received), Decimal(0))),
        "financer": _money(read.financer),
        "printedTotalRow": _money(read.printed_total),
        "unmapped": _money(sum((Decimal(u["amount"]) for u in read.unmapped), Decimal(0))),
        "unmappedLabels": [u["label"] for u in read.unmapped][:8],
        "conflicts": [{"key": c["key"], "amounts": [_money(a) for a in c["amounts"]]} for c in read.conflicts],
        "debitColumn": _money(debits),
        "creditColumn": _money(credits),
    }


def journey_statement_totals(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Every NDC and cost-sheet copy of the Journey: what it prints, what its
    rows add up to."""
    statements = []
    for doc in load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id,
                                     document_type=ACCOUNT_STATEMENT_TYPE):
        fields = doc["fields"]
        statements.append({
            "documentId": str(doc["documentId"]),
            "title": str(fields.get("statement_title") or "").strip('"') or None,
            "printed": {k: _money(_amount(fields.get(k))) for k in _STATEMENT_PRINTED
                        if _amount(fields.get(k)) not in (None, Decimal(0))},
            **statement_sums(fields),
        })
    sheets = []
    for doc in load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id,
                                     document_type=COST_SHEET_TYPE):
        fields = doc["fields"]
        sheets.append({
            "documentId": str(doc["documentId"]),
            "printed": {k: _money(_amount(fields.get(k))) for k in _COST_SHEET_PRINTED
                        if _amount(fields.get(k)) not in (None, Decimal(0))},
            "rows": len(line_rows(fields.get("line_items"))),
        })
    return {"statements": statements, "costSheets": sheets}
