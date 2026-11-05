"""The NDC's and the cost sheet's rows add up to the totals they print
(product owner 2026-11-05: "NDC rows should add up and so does cost sheet").

A printed total is the dealer's own sum. When the rows read do not give it,
a row was misread, read twice or not read, and every figure the Deal takes
from that copy is in doubt. Live on 5 Nov (Mahindra Orissa), of the NDC forms
that print a total: Utkal 73 of 172, Shivnath 30 of 54, Aditya 38 of 84 did
not add up -- rows read twice, a digit misread, a row not read.

What "adds up" means differs by dealer form, so a printed total is accepted
when the rows explain it in any of the ways a form adds them:
- Total Cost / Gross Value: the charges, less some of the discounts (Aditya
  prints its offers among the charges) and the old vehicle; Aditya's "Gross
  Value" is the vehicle's price alone.
- Net Value / Net Receivable / On-road price: the same.
- Total Receipts: the money received, with some of the discounts (Utkal's
  right-hand column counts them) and the old vehicle taken in kind.
Within ADDS_UP_TOLERANCE. A copy that is not a statement form, or whose rows
the reader mostly cannot name (an ERP settlement filed as the NDC), is not
judged here: the rows would not mean what a form's rows mean.

A cost sheet is judged once its rows are read (cost_sheet schema 1.1,
``line_items``): the on-road total is its charges less some of its
deductions. A cost sheet read before that has four figures only, and its
total includes rows not read (TCS, warranty, RSA, FASTag): not judged.

One task per booking, STATEMENT_ROWS_DO_NOT_ADD_UP, HIGH, to the PC: correct
the reading, or close it with a comment when the dealer's own sum is wrong.
It closes by itself once every copy adds up. ``journey_statement_totals`` is
also the "statement-totals" section of a journey snapshot (read only).
"""
from __future__ import annotations

import itertools
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

from audit_core.uc03_account_statement import (
    ACCOUNT_STATEMENT_TYPE,
    COST_SHEET_TYPE,
    KIND_FORM,
    StatementRead,
    _amount,
    _label,
    line_rows,
    load_ledger_documents,
    read_statement,
)

logger = structlog.get_logger(__name__)

TASK_TYPE = "STATEMENT_ROWS_DO_NOT_ADD_UP"
# A total and its rows agree within this (product owner: document differences
# are tolerated within Rs 10).
ADDS_UP_TOLERANCE = Decimal(10)
# More discount rows than this and the ways they may be split are not tried.
_MAX_SPLIT = 8
_CHARGES_TOTALS = {"total_cost": "Total Cost", "gross_value": "Gross Value"}
_NET_TOTALS = {"net_value": "Net Value"}
_RECEIPT_TOTALS = {"total_receipts": "Total Receipts"}
_COST_SHEET_TOTAL = ("total_on_road_price", "On-road total")
# A cost sheet's own total and sub-total rows, never a charge.
_COST_SHEET_SUMMARY = ("TOTAL", "GRAND TOTAL", "ON ROAD", "ONROAD", "NET AMOUNT", "SUB TOTAL", "SUBTOTAL")

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


def _plain(differences: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if differences is None:
        return None
    return [{k: (_money(v) if isinstance(v, Decimal) else v) for k, v in d.items()} for d in differences]


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
            "differences": _plain(statement_differences(fields)),
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
            "differences": _plain(cost_sheet_differences(fields)),
        })
    return {"statements": statements, "costSheets": sheets}


# ── the check (pure) ─────────────────────────────────────────────────────────
def _splits(amounts: list[Decimal]) -> set[Decimal]:
    """Every sum of some of the amounts (none of them, all of them, or any
    part): the share a form may print on one side or the other."""
    amounts = amounts[:_MAX_SPLIT]
    return {sum(c, Decimal(0)) for n in range(len(amounts) + 1) for c in itertools.combinations(amounts, n)}


def _difference(label: str, printed: Decimal, ways: list[Decimal]) -> dict[str, Any] | None:
    """None when one of the ways the rows add up gives the printed total, else
    the way nearest to it."""
    nearest = min(ways, key=lambda way: abs(printed - way))
    if abs(printed - nearest) <= ADDS_UP_TOLERANCE:
        return None
    return {"total": label, "printed": printed, "rows": nearest, "difference": printed - nearest}


def understood(read: StatementRead) -> bool:
    """A statement form whose rows the reader names (at most half the charges'
    worth left unnamed)."""
    if read.kind != KIND_FORM:
        return False
    unnamed = sum((Decimal(u["amount"]) for u in read.unmapped), Decimal(0))
    charges = sum(read.components.values(), Decimal(0))
    return charges > 0 and unnamed <= charges / 2


def statement_differences(fields: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The printed totals of one NDC copy its rows do not give. None when the
    copy is not judged (no rows, not a form the reader understands, no total)."""
    lines = line_rows(fields.get("line_items"))
    if not lines:
        return None
    read = read_statement(lines)
    if not understood(read):
        return None
    charges = sum(read.components.values(), Decimal(0))
    old = read.old_vehicle_value or Decimal(0)
    shares = _splits(list(read.discounts.values()))
    less = [charges - share - o for share in shares for o in {Decimal(0), old}]
    received = [read.receipts + share + o for share in shares for o in {Decimal(0), old, -old}]
    ex_showroom = read.components.get("ex_showroom_price")
    printed: list[tuple[str, Decimal, list[Decimal]]] = []
    for key, label in _CHARGES_TOTALS.items():
        if (amount := _amount(fields.get(key))) not in (None, Decimal(0)):
            ways = less + ([ex_showroom] if key == "gross_value" and ex_showroom else [])
            printed.append((label, abs(amount), ways))
    net = _amount(fields.get("net_value"))
    if net not in (None, Decimal(0)):
        printed.append((_NET_TOTALS["net_value"], abs(net), less))
    elif read.printed_total:
        printed.append(("Net Value", read.printed_total, less))
    for key, label in _RECEIPT_TOTALS.items():
        if (amount := _amount(fields.get(key))) not in (None, Decimal(0)):
            printed.append((label, abs(amount), received))
    if not printed:
        return None
    return [d for label, amount, ways in printed if (d := _difference(label, amount, ways)) is not None]


def reading_error(fields: dict[str, Any]) -> Decimal | None:
    """How far one NDC copy's rows are from its printed totals, all added (0 when
    it adds up), from its fields as stored; None when it is not judged."""
    from audit_core.uc03_account_statement import _discount_detail_rows

    fields = {k: v for k, v in fields.items() if v not in (None, "", "null")}
    details = _discount_detail_rows(fields)
    if details:
        fields["line_items"] = [*line_rows(fields.get("line_items")), *details]
    differences = statement_differences(fields)
    return None if differences is None else sum((abs(d["difference"]) for d in differences), Decimal(0))


def cost_sheet_rows(fields: dict[str, Any]) -> tuple[Decimal, list[Decimal]] | None:
    """A cost sheet's charges and deductions from its rows, None when its rows
    were not read."""
    rows = line_rows(fields.get("line_items"))
    charges, deductions = Decimal(0), []
    named = False
    for row in rows:
        amount = _amount(row.get("amount"))
        label = _label(row.get("particulars_raw"))
        if amount in (None, Decimal(0)) or any(label.startswith(word) for word in _COST_SHEET_SUMMARY):
            continue
        named = True
        if str(row.get("deduction")).strip().lower() in ("true", "1", "yes") or amount < 0:
            deductions.append(abs(amount))
        else:
            charges += amount
    return (charges, deductions) if named else None


def cost_sheet_differences(fields: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The cost sheet's on-road total against its rows; None when not judged."""
    total = _amount(fields.get(_COST_SHEET_TOTAL[0]))
    rows = cost_sheet_rows(fields)
    if total in (None, Decimal(0)) or rows is None:
        return None
    charges, deductions = rows
    found = _difference(_COST_SHEET_TOTAL[1], abs(total), [charges - share for share in _splits(deductions)])
    return [found] if found else []


# ── the journey ──────────────────────────────────────────────────────────────
def _pages(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, int]:
    rows = connection.execute(
        text("SELECT di_document_id, min(page_number) AS page FROM auditcore.p2_document_queue "
             "WHERE tenant_id=:t AND journey_id=:j AND di_document_id IS NOT NULL GROUP BY di_document_id"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    return {str(r["di_document_id"]): int(r["page"]) for r in rows if r["page"] is not None}


def copies_not_adding_up(connection: Connection, *, tenant_id: str, journey_id: UUID) -> tuple[int, list[dict[str, Any]]]:
    """(copies judged, the copies whose rows do not give a printed total), page order."""
    pages = _pages(connection, tenant_id=tenant_id, journey_id=journey_id)
    judged = 0
    out = []
    for document_type, name, check in ((ACCOUNT_STATEMENT_TYPE, "NDC", statement_differences),
                                       (COST_SHEET_TYPE, "Cost sheet", cost_sheet_differences)):
        for doc in load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id,
                                         document_type=document_type):
            differences = check(doc["fields"])
            if differences is None:
                continue
            judged += 1
            if differences:
                out.append({"documentId": str(doc["documentId"]), "documentType": document_type, "document": name,
                            "pageNumber": pages.get(str(doc["documentId"])), "differences": differences})
    return judged, sorted(out, key=lambda d: (d["pageNumber"] is None, d["pageNumber"] or 0, d["documentId"]))


def _rupees(value: Decimal) -> str:
    whole = int(abs(value).quantize(Decimal(1)))
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        digits = ",".join([head, *groups, tail]) if head else ",".join([*groups, tail])
    return f"Rs {digits}"


def describe(copies: list[dict[str, Any]]) -> str:
    parts = []
    for copy in copies:
        page = f" (page {copy['pageNumber']})" if copy["pageNumber"] else ""
        totals = ", ".join(
            f"{d['total']} printed {_rupees(d['printed'])}, its rows add up to {_rupees(d['rows'])} "
            f"({_rupees(d['difference'])} apart)"
            for d in copy["differences"])
        parts.append(f"{copy['document']}{page}: {totals}")
    return "; ".join(parts)


def _task_key(journey_id: UUID) -> str:
    return f"statement-rows-add-up:{journey_id}"


def sync_statement_totals(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Raise, refresh or close the booking's one task. Never raises."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh, resolve_if_open

    try:
        with connection.begin_nested():
            judged, copies = copies_not_adding_up(connection, tenant_id=tenant_id, journey_id=journey_id)
            if copies:
                raise_or_refresh(
                    connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=_task_key(journey_id),
                    task_type=TASK_TYPE, source_type="RESOLVER", source_code=TASK_TYPE,
                    title="NDC / cost sheet rows do not add up to their printed total",
                    description=(
                        "The rows read from these copies do not add up to the total the dealer printed on them: "
                        + describe(copies) + ". A row was misread, read twice or not read: correct the reading "
                        "of the copy. If the page was read right and the dealer's own sum on it is wrong, close "
                        "this task with a comment saying so: it then stands on the Audit Summary as the "
                        "dealer's arithmetic."
                    ),
                    reference={"sourceCode": TASK_TYPE, "copies": [
                        {"documentId": c["documentId"], "documentType": c["documentType"],
                         "differences": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}
                                         for d in c["differences"]]} for c in copies]},
                    severity="HIGH", registry=get_registry(),
                )
            else:
                resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id,
                                dedupe_key=_task_key(journey_id), evidence={"copiesNotAddingUp": 0})
            return {"judged": judged, "copies": len(copies)}
    except Exception:
        logger.warning("statement_totals_failed", tenant_id=tenant_id, journey_id=str(journey_id), exc_info=True)
        return {"error": True}
