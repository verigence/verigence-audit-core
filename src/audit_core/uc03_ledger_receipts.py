"""Money the customer ledger and the NDC record as received with no payment
receipt on file: the receipts that are missing, never payments.

Product owner 2026-11-17: "payment receipts should contain only the payment
receipts.. it shouldn't have any data coming from NDC". A payment is a payment
receipt on file, and nothing else: not a line of the NDC, not a posting of the
customer ledger. From 2 to 16 Nov 2026 such a line or posting with no receipt
on file was itself counted as a payment and listed among the payment receipts
("NDC (no receipt on file)"), on 216 bookings by 17 Nov. That put the NDC's
own reading among the receipts (live, UTK-SA-261103-012: receipts 2045 of
Rs 11,000 and 2047 of Rs 5,00,000 are on file; the NDC's lines were read as
"CHAN-2045 Rs 1,100" and "CHAN-2047 Rs 4,50,000" and stood as Rs 4,51,100 of
further money received) and made the owner's own check -- the payment receipts
equal the NDC's net value -- compare the NDC with itself.

What the NDC and the ledger are used for (product owner 2026-11-02, 11-15,
11-16): verification. Each credit posting of the ledger that is money in -- a
Receipt voucher, or a journal transfer from another ledger (a relative's
account) -- and each received line of the NDC, that no receipt on file stands
for (the same receipt number, else the same amount, each receipt standing for
one posting, the nearest date first), is a receipt the PC is asked to upload
(PAYMENT_RECEIPT_MISSING). It is read each time, never stored. Credit notes
(benefits), the vehicle sale and round-off are not money received. A posting
equal to a loan amount already known (the finance record's disbursement, the
statement's financier row, the bank letter's sanction), or a journal from the
financier, is the loan, which the loan rule counts and a system remark
states: no receipt is asked for it.
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
_NOT_MONEY = re.compile(r".*(ROUND(ING|ED)? ?OFF|CREDIT ?NOTE|DISCOUNT|BONUS|SCHEME|OFFER).*")
_CASH = re.compile(r".*\bCASH\b.*")
_BANK = re.compile(r".*\b(BANK|NEFT|RTGS|IMPS|UPI|POS)\b.*")
# A journal from the financier (MMFSL, "... FINANCE ...", "... FIN SERV ...").
_FINANCIER = re.compile(r".*\b(FINANCE|FINANCIAL|FIN\.? ?SERV\w*|MMFSL|CAPITAL|LEASING)\b.*")


def is_loan_net_of_charges(amount: Decimal, loan: Decimal) -> bool:
    """The amount is the loan, or its disbursement net of the lender's
    charges: between 98% of the loan and the loan (a rupee over allowed)."""
    return Decimal(loan) * _LOAN_NET_SHARE <= Decimal(amount) <= Decimal(loan) + 1


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
                and abs(Decimal(receipts[i]["amount"]) - posting["amount"]) <= 1)
            # the NDC writes only the receipt's serial ("RECEIPT 0608" for
            # PB/26-27/0608; independent check 2026-11-04): the same receipt
            # when the amounts agree within 1% (a digit misread)
            or (len(number) >= 3 and _number(receipts[i]["receipt_number"]).endswith(number)
                and abs(Decimal(receipts[i]["amount"]) - posting["amount"])
                <= max(Decimal(1), Decimal(receipts[i]["amount"]) / 100)))), None)
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
    # (against every receipt on file: one another line took by its amount is on file all the same)
    return _combined(_same_serial(left, receipts), [receipts[i] for i in free])


def _serial(value: Any) -> str | None:
    """The running number a receipt number ends in, when it has four digits or
    more, without its leading zeros ("CDAM/2627/02045" and "CHAN-2045" both
    end in 2045; "AMC-B/26509/26-27" ends in its year, not a serial)."""
    digits = re.search(r"(\d+)\D*$", str(value or ""))
    return digits.group(1).lstrip("0") or "0" if digits and len(digits.group(1)) >= 4 else None


def _same_serial(postings: list[dict[str, Any]], receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Of the postings left, those whose receipt is not on file under the same
    running number either. The NDC is handwritten: its receipt number's letters
    and its amount are misread ("CHAN-2045 Rs 1,100" for CDAM/2627/02045 of
    Rs 11,000, live 2026-11-17), so the number and the amount both failed to
    find the receipt that is on file. A receipt on file ending in the same
    running number of four digits or more is that receipt: it is not missing,
    whatever amount the line was read as."""
    serials = {_serial(r["receipt_number"]) for r in receipts} - {None}
    return [p for p in postings if _serial(p["number"]) not in serials]


# The rupees an NDC's received lines may run over its total (rounding).
_NDC_TOLERANCE = Decimal(10)

# The most receipts one posting is matched against.
_MAX_COMBINED = 4


def _combined(postings: list[dict[str, Any]], receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Of the postings left, those no set of the receipts left adds up to:
    the dealer may post several receipts as one transfer (live 2026-11-03:
    three receipts of Rs 5,000, 11,000 and 70,000 posted as one Rs 86,000
    journal from the customer's ledger). Each receipt stands once."""
    receipts = list(receipts)
    left = list(postings)
    for posting in sorted(postings, key=lambda p: -p["amount"]):
        hit = _subset(posting["amount"], [Decimal(r["amount"]) for r in receipts])
        if hit is not None:
            receipts = [r for i, r in enumerate(receipts) if i not in hit]
            left = [p for p in left if p is not posting]
    return left


def _subset(total: Decimal, amounts: list[Decimal]) -> set[int] | None:
    """The indexes of the fewest amounts (two to _MAX_COMBINED) that add up to
    ``total`` within a rupee; None when none do."""
    from itertools import combinations

    for size in range(2, min(_MAX_COMBINED, len(amounts)) + 1):
        for picked in combinations(range(len(amounts)), size):
            if abs(sum((amounts[i] for i in picked), Decimal(0)) - total) <= 1:
                return set(picked)
    return None


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


def unreceipted_postings(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, list[dict[str, Any]]]:
    """The customer ledger's money-in postings, and the NDC's received lines,
    that no receipt on file stands for, each with its page as its document:
    ``customer`` -- not the loan (``ledger_receipts``); ``loan`` -- the loan
    (an amount equal to a loan figure known for the booking, or the
    financier's journal credit), received with no receipt on file (product
    owner 2026-11-16: no task for it, a system remark)."""
    from audit_core.uc03_account_statement import (
        KIND_TALLY,
        combined_tally_rows,
        journey_statement_received_lines,
        line_rows,
        load_ledger_documents,
        statement_kind,
    )

    documents = load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
    tallies = [(d, rows) for d in documents for rows in [line_rows(d["fields"].get("line_items"))]
               if statement_kind(rows) == KIND_TALLY]
    ndc_lines, ndc_total, ndc_document = journey_statement_received_lines(
        connection, tenant_id=tenant_id, journey_id=journey_id)
    if not tallies and not ndc_lines:
        return {"customer": [], "loan": []}
    receipts = _on_file(connection, tenant_id=tenant_id, journey_id=journey_id)
    loans: list[Decimal] | None = None
    out: list[dict[str, Any]] = []
    loan_out: list[dict[str, Any]] = []

    def not_the_loan(posting: dict[str, Any]) -> bool:
        nonlocal loans
        loans = loans if loans is not None else _loan_amounts(connection, tenant_id=tenant_id, journey_id=journey_id)
        # The loan: the amount known, or the disbursement net of the lender's
        # charges, a little under it (live: Rs 12,50,000 credited against a
        # sanction of Rs 12,57,000).
        return not any(is_loan_net_of_charges(posting["amount"], loan) for loan in loans)

    if tallies:
        origin = max((d for d, _ in tallies), key=lambda d: (
            d.get("linkedAtUtc").timestamp() if hasattr(d.get("linkedAtUtc"), "timestamp") else 0.0))
        for posting in _match(money_in(combined_tally_rows(tallies)), receipts):
            located = {**posting, "documentId": str(origin["documentId"])}
            if (_JOURNAL_VOUCHER.fullmatch(posting["voucherType"])
                    and _FINANCIER.fullmatch(_label(posting["particulars"]))) or not not_the_loan(posting):
                loan_out.append({**located, "documentLabel": "Customer ledger"})
            else:
                out.append({**located, "documentLabel": "Customer ledger (no receipt on file)"})
    if ndc_lines:
        # The NDC's received lines no receipt on file and no ledger posting
        # already counted stands for (product owner 2026-11-04): each money
        # received once, whichever document shows it. Never more than the
        # NDC's own received total leaves unexplained by the receipts on file
        # and the ledger: when its lines and the receipts are split
        # differently, the lines left are not more money.
        taken = receipts + [{"receipt_number": p["number"], "receipt_date": p["date"], "amount": p["amount"]}
                            for p in out]
        left = [p for p in _match(ndc_lines, taken) if not_the_loan(p)]
        on_file = sum((Decimal(r["amount"]) for r in receipts if not_the_loan({"amount": Decimal(r["amount"])})),
                      Decimal(0)) + sum((p["amount"] for p in out), Decimal(0))
        # (a receipt matched within 1% of its NDC line leaves that 1% of slack)
        if sum((p["amount"] for p in left), Decimal(0)) <= ndc_total - on_file + max(_NDC_TOLERANCE, on_file / 100):
            out.extend({**p, "documentId": ndc_document, "documentLabel": "NDC (no receipt on file)"} for p in left)
        # The loan on the NDC with no receipt on file, unless the ledger
        # already showed the same credit.
        shown = taken + [{"receipt_number": p["number"], "receipt_date": p["date"], "amount": p["amount"]}
                         for p in loan_out]
        loan_out.extend({**p, "documentId": ndc_document, "documentLabel": "NDC"}
                        for p in _match(ndc_lines, shown) if not not_the_loan(p))
    return {"customer": out, "loan": loan_out}


def missing_receipts(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """The receipts the NDC and the customer ledger show and the file does not
    have: their money-in lines no receipt on file stands for and that are not
    the loan, each with its page as its document. Never payments: nothing here
    is counted as money received or listed among the payment receipts (product
    owner 2026-11-17)."""
    return unreceipted_postings(connection, tenant_id=tenant_id, journey_id=journey_id)["customer"]


def loan_without_receipt(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """The loan the NDC or the customer ledger shows received, with no
    payment receipt on file for it."""
    return unreceipted_postings(connection, tenant_id=tenant_id, journey_id=journey_id)["loan"]


# Rows of a ledger printout that repeat the postings above them.
_CARRIED = re.compile(r"(CLOSING BALANCE|TOTAL|GRAND TOTAL|CARRIED OVER|BROUGHT FORWARD|C/F|B/F|C/O|B/O)\b.*")


def ledger_balance(rows: list[dict[str, Any]]) -> Decimal | None:
    """What the customer ledger says the customer owes: its debits less its
    credits (a negative figure is money the customer paid over). The
    closing-balance, total and carried-over rows repeat the postings and are
    not added. None when the ledger has no posting."""
    total, posted = Decimal(0), False
    for row in rows:
        if _CARRIED.fullmatch(_label(row.get("particulars_raw"))):
            continue
        debit, credit = _amount(row.get("debit_amount")), _amount(row.get("credit_amount"))
        if debit:
            total += abs(debit)
            posted = True
        if credit:
            total -= abs(credit)
            posted = True
    return total if posted else None


def customer_ledger_balance(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any] | None:
    """The Tally customer ledger's balance, for the Audit Summary's check of
    the deal's balance (product owner 2026-11-03: the ledger rows are not shown
    on screen; they are used to verify in the Audit Summary). None with no
    Tally ledger on file."""
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
        return None
    balance = ledger_balance(combined_tally_rows(tallies))
    if balance is None:
        return None
    return {"balance": balance, "documentId": str(tallies[0][0]["documentId"])}


def ledger_financier_credit(rows: list[dict[str, Any]]) -> Decimal | None:
    """The financier's journal posting credited to the customer (its D/O), the
    largest when several; None when the ledger shows none."""
    amounts = [p["amount"] for p in money_in(rows)
               if _JOURNAL_VOUCHER.fullmatch(p["voucherType"]) and _FINANCIER.fullmatch(_label(p["particulars"]))]
    return max(amounts) if amounts else None


def ledger_loan_credit(connection: Connection, *, tenant_id: str, journey_id: UUID, loan: Decimal) -> Decimal | None:
    """The money-in posting of the customer ledger that is the loan: the
    largest one between the loan amount less the lender's charges (2%) and
    the loan amount. None when the ledger shows none."""
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
        return None
    amounts = [p["amount"] for p in money_in(combined_tally_rows(tallies))
               if is_loan_net_of_charges(p["amount"], loan)]
    return max(amounts) if amounts else None


__all__ = [
    "customer_ledger_balance",
    "is_loan_net_of_charges",
    "ledger_balance",
    "ledger_financier_credit",
    "ledger_loan_credit",
    "loan_without_receipt",
    "missing_receipts",
    "money_in",
    "unreceipted_postings",
]
