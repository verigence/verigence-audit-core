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
for it: the same receipt number, else the same amount (each receipt standing
for one posting, the nearest date first). It is
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

# A disbursement is the sanction less the lender's charges: within 2%.
_LOAN_NET_SHARE = Decimal("0.98")
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


def _number(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _match(postings: list[dict[str, Any]], receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The postings no receipt on file stands for. Each receipt stands for one
    posting at most: first by its number (the receipt's own reading may drop
    a leading letter, "B/26-27/0672" for "PB/26-27/0672"), then by its amount,
    the nearest date first -- whatever the gap: the dealer posts a transfer
    from another ledger days or weeks after the receipt (live 2026-11-02: a
    Rs 70,000 cash booking amount of 03-10 posted as "SABITA RANI DASH" on
    24-10; a Rs 21,000 receipt posted as "BARADA PRASAN NAYAK")."""
    free = list(range(len(receipts)))
    left: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for posting in postings:
        number = _number(posting["number"])
        hit = next((i for i in free if number and len(_number(receipts[i]["receipt_number"])) >= 4 and (
            number == _number(receipts[i]["receipt_number"])
            or (number.endswith(_number(receipts[i]["receipt_number"]))
                and abs(Decimal(receipts[i]["amount"]) - posting["amount"]) <= 1))), None)
        if hit is not None:
            free.remove(hit)
        else:
            unmatched.append(posting)
    for posting in unmatched:
        same = [i for i in free if abs(Decimal(receipts[i]["amount"]) - posting["amount"]) <= 1]
        if not same:
            left.append(posting)
            continue

        def gap(i: int, posting: dict[str, Any] = posting) -> int:
            day = _day(receipts[i]["receipt_date"])
            return abs((day - posting["date"]).days) if day and posting["date"] else 0

        free.remove(min(same, key=gap))
    return left


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
    for posting in _match(money_in(combined_tally_rows(tallies)), receipts):
        if _JOURNAL_VOUCHER.fullmatch(posting["voucherType"]) and _FINANCIER.fullmatch(_label(posting["particulars"])):
            continue
        loans = loans if loans is not None else _loan_amounts(connection, tenant_id=tenant_id, journey_id=journey_id)
        # The loan: the amount known, or the disbursement net of the lender's
        # charges, a little under it (live: Rs 12,50,000 credited against a
        # sanction of Rs 12,57,000).
        if any(loan * _LOAN_NET_SHARE <= posting["amount"] <= loan + 1 for loan in loans):
            continue
        out.append({**posting, "documentId": str(origin["documentId"])})
    return out


__all__ = ["ledger_receipts", "money_in"]
