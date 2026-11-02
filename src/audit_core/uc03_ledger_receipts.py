"""Money the customer ledger records as received, as payments (2026-11-02).

Product owner rule (2 Nov 2026): any line item read on a billing document
counts, the customer ledger is one, and the customer ledger is used for the
payment verification. A receipt the dealer posted in the Tally customer ledger
whose own receipt is not in the file (live: Bhikari Singh's Rs 11,000 booking
amount of 23-09; Chinmay Kumar Gajendra's Rs 5,82,229 and Rs 12,50,000 of
23-09) is money received all the same.

Each credit posting of the ledger that is money in -- a Receipt voucher, or a
journal transfer from another ledger (a relative's account) -- is a payment of
the booking, the ledger its document, unless a receipt on file already stands
for it: the same receipt number, or the same amount within three days. It is
read from the ledger each time (payments are one row per document, and the
ledger is one document with many postings), never stored. Credit notes
(benefits), the vehicle sale and round-off are not money received. A posting
equal to a loan amount already known (the finance record's disbursement, the
statement's financier row, the bank letter's sanction), or a journal from the
financier, is the loan, which the loan rule counts: never a second payment
beside it.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

_DAYS = 3
_RECEIPT_VOUCHER = re.compile(r".*RECEIPT.*")
_JOURNAL_VOUCHER = re.compile(r".*JOURNAL.*")
_NOT_MONEY = re.compile(r".*(ROUND ?OFF|CREDIT ?NOTE|DISCOUNT|BONUS|SCHEME|OFFER).*")
_CASH = re.compile(r".*\bCASH\b.*")
_BANK = re.compile(r".*\b(BANK|NEFT|RTGS|IMPS|UPI|POS)\b.*")
# A journal from the financier (MMFSL, "... FINANCE ...", "... FIN SERV ...").
_FINANCIER = re.compile(r".*\b(FINANCE|FINANCIAL|FIN\.? ?SERV\w*|MMFSL|CAPITAL|LEASING)\b.*")


def _label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").upper()).strip()


def _amount(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except ArithmeticError:
        return None


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def money_in(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ledger's postings that are money received: Receipt vouchers and
    journal transfers credited to the customer."""
    out = []
    for row in rows:
        credit = _amount(row.get("credit_amount"))
        if credit is None or credit <= 0:
            continue
        voucher, particulars = _label(row.get("voucher_type")), _label(row.get("particulars_raw"))
        if _NOT_MONEY.fullmatch(particulars) or _NOT_MONEY.fullmatch(voucher):
            continue
        if not (_RECEIPT_VOUCHER.fullmatch(voucher) or _JOURNAL_VOUCHER.fullmatch(voucher)):
            continue
        mode = "CASH" if _CASH.fullmatch(particulars) else "BANK_TRANSFER" if _BANK.fullmatch(particulars) else "OTHERS"
        out.append({"amount": credit, "date": _day(row.get("entry_date")),
                    "number": str(row.get("voucher_number") or "").strip() or None,
                    "particulars": str(row.get("particulars_raw") or "").strip(), "voucherType": voucher,
                    "mode": mode})
    return out


def _on_file(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(
        text(
            """
            SELECT p.receipt_number, p.receipt_date, p.amount
            FROM auditcore.payments p
            JOIN auditcore.evidence e
              ON e.tenant_id = p.tenant_id AND e.journey_id = p.journey_id
             AND e.di_document_id = p.source_di_document_id AND e.association_status = 'ACTIVE'
            WHERE p.tenant_id = :t AND p.journey_id = :j AND p.amount > 0 AND p.status_source = 'EVIDENCE'
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()]


def _stands_for(posting: dict[str, Any], receipts: list[dict[str, Any]]) -> bool:
    """A receipt on file already stands for this posting: the same receipt
    number (the receipt's own reading may drop a leading letter, "B/26-27/0672"
    for "PB/26-27/0672"), or the same amount within three days."""
    number = re.sub(r"[^A-Z0-9]", "", (posting["number"] or "").upper())
    for receipt in receipts:
        same_amount = abs(Decimal(receipt["amount"]) - posting["amount"]) <= 1
        theirs = re.sub(r"[^A-Z0-9]", "", str(receipt["receipt_number"] or "").upper())
        if number and len(theirs) >= 4 and (number == theirs or (number.endswith(theirs) and same_amount)):
            return True
        if same_amount:
            day = _day(receipt["receipt_date"])
            if day is None or posting["date"] is None or abs((day - posting["date"]).days) <= _DAYS:
                return True
    return False


def _loan_amounts(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[Decimal]:
    """The loan amounts known for the booking: disbursed on the finance
    record, the statement's financier row, the bank letter's sanction."""
    from audit_core.uc03_finance_disbursement_resolution import stated_loan_amounts

    amounts = [Decimal(r[0]) for r in connection.execute(
        text("SELECT loan_disbursement_amount FROM auditcore.finance_records "
             "WHERE tenant_id=:t AND journey_id=:j AND loan_disbursement_amount IS NOT NULL"),
        {"t": tenant_id, "j": journey_id},
    ).all()]
    amounts += [Decimal(str(s["amount"])) for s in stated_loan_amounts(connection, tenant_id=tenant_id,
                                                                       journey_id=journey_id)
                if s.get("amount") is not None]
    return amounts


def ledger_receipts(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """The customer ledger's money-in postings no receipt on file stands for
    and that are not the loan, each with the ledger page as its document."""
    from audit_core.uc03_account_statement import (
        KIND_TALLY,
        combined_tally_rows,
        line_rows,
        load_ledger_documents,
        statement_kind,
    )

    documents = load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
    tallies = [(d, rows) for d in documents for rows in [line_rows(d["fields"].get("line_items"))]
               if statement_kind(rows) == KIND_TALLY]
    if not tallies:
        return []
    origin = max((d for d, _ in tallies), key=lambda d: (
        d.get("linkedAtUtc").timestamp() if hasattr(d.get("linkedAtUtc"), "timestamp") else 0.0))
    receipts = _on_file(connection, tenant_id=tenant_id, journey_id=journey_id)
    loans: list[Decimal] | None = None
    out = []
    for posting in money_in(combined_tally_rows(tallies)):
        if _stands_for(posting, receipts):
            continue
        if _JOURNAL_VOUCHER.fullmatch(posting["voucherType"]) and _FINANCIER.fullmatch(_label(posting["particulars"])):
            continue
        loans = loans if loans is not None else _loan_amounts(connection, tenant_id=tenant_id, journey_id=journey_id)
        if any(abs(loan - posting["amount"]) <= 1 for loan in loans):
            continue
        out.append({**posting, "documentId": str(origin["documentId"])})
    return out


__all__ = ["ledger_receipts", "money_in"]
