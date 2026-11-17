"""The payment receipts on file set against the money the NDC shows received;
the customer ledger's postings and balance, for verification.

Product owner 2026-11-17: "payment receipts should contain only the payment
receipts.. it shouldn't have any data coming from NDC". A payment is a payment
receipt on file, and nothing else: not a line of the NDC, not a posting of the
customer ledger. And the NDC's payments are checked as sums, never line by
line (receipts_against_ndc, below): the receipts on file are added up and set
against the money the NDC shows received; short of it, one task asks the PC
for the receipts; short by about the loan only, a system remark says the loan
was received as per the NDC with no receipt on file.

The customer ledger (Tally) verifies (product owner 2026-11-15): its balance
against the Deal's (customer_ledger_balance), its financier's credit as the
loan where nothing else gives one (ledger_financier_credit, ledger_loan_credit).
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

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


# ── the payment receipts set against the NDC (product owner 2026-11-17) ───────
#
# "NDC has to be there to read all the line items ... all these values should
# add up as one sum; now if NDC has payment receipt info, add up all those
# numbers and verify if two sums are same; additionally check the payment
# receipts and add up those, if payment receipts are less then just raise that
# task. Here it may be that customer has taken up loan. If payment receipt is
# equal to the loan amount ... don't raise task for this and just add system
# remark."
#
# Three sums, never a line of the NDC set against a receipt one by one:
# 1. the NDC's charges less its discounts give its net value
#    (uc03_statement_totals, the "NDC rows do not add up" task);
# 2. the NDC's payments give its printed Total Receipts -- read as one sum,
#    which is how a handwritten line is caught (live UTK-SA-261103-012, Avijit
#    Biswal: "CHAN-2045 Rs 1,100" and "CHAN-2047 Rs 4,50,000" read for receipts
#    of Rs 11,000 and Rs 4,00,000; the lines add up to Rs 40,100 over the printed
#    total). While they do not, the NDC's money is not known, and no receipt is
#    asked for: the reading is the one task (one cause, one task);
# 3. the payment receipts on file are added up and set against the money the
#    NDC shows received: its customer payments and the loan. What the dealer
#    writes in the payments column that is not money -- an offer (Aditya prints
#    a Rs 4,000 corporate offer among the receipts), the old vehicle taken in
#    kind -- is not money a receipt can show and is not asked for.
# The loan with no receipt on file standing for it (none linked to it, none of
# its amount): the loan received as per the NDC, a system remark, no task. The
# receipts short of the rest: one task for what is short.
#
# From 2 to 17 Nov 2026 each NDC line was matched to a receipt by its number,
# then its amount; a misread line was then a receipt "missing" (the
# Rs 4,50,000 above). The customer ledger (Tally) is not used for this: it is
# read to verify the NDC (product owner 2026-11-15), and the NDC is the source.
RECEIPTS_TOLERANCE = Decimal(100)


def _section_copy(forms: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> dict[str, Any] | None:
    """The NDC copy whose payments are read: the standing copy (the one the
    Deal reads), else the newest other one in use that shows money. Never
    added across copies."""
    from audit_core.uc03_account_statement import combine_statements, read_statement

    _, copies = combine_statements(forms)
    if copies.primary is None:
        return None
    rows_of = {id(d): rows for d, rows in forms}
    order = [copies.primary, *[d for d in copies.used if d is not copies.primary]]
    for document in order:
        read = read_statement(rows_of[id(document)])
        if read.received or read.financer:
            return {"document": document, "read": read}
    return {"document": copies.primary, "read": read_statement(rows_of[id(copies.primary)])}


def ndc_money(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any] | None:
    """What the NDC shows: its net value, its printed Total Receipts, and the
    money received (customer payments and the loan) -- with ``held`` saying why
    the money cannot be taken from it yet. None when no NDC form is on file."""
    from audit_core.uc03_account_statement import (
        KIND_FORM,
        line_rows,
        load_ledger_documents,
        statement_kind,
        statement_total,
    )
    from audit_core.uc03_statement_totals import _rupees, _splits, statement_differences

    forms = [(d, rows) for d in load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
             for rows in [line_rows(d["fields"].get("line_items"))] if statement_kind(rows) == KIND_FORM]
    if not forms:
        return None
    picked = _section_copy(forms)
    if picked is None:
        return None
    document, read = picked["document"], picked["read"]
    fields = document["fields"]
    received = sum((amount for _, amount in read.received), Decimal(0))
    loan = read.financer or Decimal(0)
    # The financier's payment listed among the received lines too (Shivnath's
    # offer letter plans the finance and lists its receipt): one payment,
    # counted once -- and still the loan.
    listed = bool(loan) and any(abs(amount - loan) <= 1 for _, amount in read.received)
    customer = received - loan if listed else received
    printed = _amount(fields.get("total_receipts"))
    printed = abs(printed) if printed else None
    net = statement_total(fields, read)
    differences = statement_differences(fields)
    # A payments column more than the receipts tolerance away from its printed total: a line is
    # misread, and the NDC's money is not known (a smaller gap cannot raise a task on its own).
    payments_off = next((d for d in differences or []
                         if d["total"] == "Total Receipts" and abs(d["difference"]) > RECEIPTS_TOLERANCE), None)
    held = held_for = None
    if differences is None:
        held, held_for = "The NDC's rows are not read well enough to tell its payments.", "NOT_READ"
    elif payments_off is not None:
        held, held_for = (f"The NDC's payment lines add up to {_rupees(payments_off['rows'])} as read; it prints "
                          f"Total Receipts of {_rupees(payments_off['printed'])}. The payment receipts are compared "
                          "once the NDC's reading is corrected (task: NDC rows do not add up)."), "PAYMENTS_OFF"
    elif printed is None and (customer or loan):
        # No Total Receipts printed: the payments are set against the other sum, the net value
        # (product owner: "verify if two sums are same"), in any of the ways the form adds them.
        old = read.old_vehicle_value or Decimal(0)
        ways = [read.receipts + share + o for share in _splits(list(read.discounts.values()))
                for o in {Decimal(0), old, -old}]
        if net is None:
            held, held_for = ("The NDC prints neither Total Receipts nor a net value, so its payment lines cannot "
                              "be checked as one sum."), "NO_TOTAL"
        elif min(abs(net - way) for way in ways) > RECEIPTS_TOLERANCE:
            held, held_for = ("The NDC prints no Total Receipts, and its payment lines do not give its net value "
                              f"of {_rupees(net)}: the payment receipts are compared once its reading is "
                              "checked."), "NO_TOTAL"
    return {"documentId": str(document["documentId"]), "net": net, "printedReceipts": printed,
            "customer": customer, "loan": loan, "money": customer + loan, "held": held, "heldFor": held_for,
            # the NDC's own showing of a balance: net value against its payments column
            "ndcBalance": (net - printed) if net is not None and printed is not None else None}


def _receipts_on_file(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """The payment receipts on file, each counted once (the Deal's own rule:
    uc03_p2_journey360._paid): every amount, and the total of those linked to
    the loan."""
    from audit_core.uc03_p2_journey360 import _paid

    paid = _paid(connection, tenant_id=tenant_id, journey_id=journey_id)
    counted = [Decimal(a) for a in paid["countedAmounts"]]
    customer = [Decimal(a) for a in paid["customerAmounts"]]
    return {"amounts": counted, "total": sum(counted, Decimal(0)),
            "loanLinked": sum(counted, Decimal(0)) - sum(customer, Decimal(0))}


def _loan_receipt(loan: Decimal, on_file: dict[str, Any]) -> Decimal | None:
    """The receipt on file that stands for the loan: those linked to it on the
    finance record, else the one of the loan's amount or the loan less the
    lender's charges (2%) -- its amount; None when no receipt stands for it."""
    if on_file["loanLinked"] >= loan * _LOAN_NET_SHARE - RECEIPTS_TOLERANCE:
        return on_file["loanLinked"]
    found = [amount for amount in on_file["amounts"] if is_loan_net_of_charges(amount, loan)]
    return max(found) if found else None


def receipts_against_ndc(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """The payment receipts on file against the money the NDC shows received.

    ``status``: NO_NDC (no NDC form on file), HELD (the NDC's payments do not
    add up to the total it prints: not judged), COVERED, or SHORT -- ``short``
    the receipts not on file, the loan apart. ``loanWithoutReceipt`` the loan
    the NDC shows received that no receipt on file stands for (a system remark,
    no task: product owner 2026-11-16 and 2026-11-17)."""
    ndc = ndc_money(connection, tenant_id=tenant_id, journey_id=journey_id)
    if ndc is None:
        return {"status": "NO_NDC", "short": Decimal(0), "loanWithoutReceipt": Decimal(0)}
    on_file = _receipts_on_file(connection, tenant_id=tenant_id, journey_id=journey_id)
    out = {**ndc, "onFile": on_file["total"], "receiptsOnFile": len(on_file["amounts"]), "short": Decimal(0),
           "loanWithoutReceipt": Decimal(0)}
    if ndc["held"]:
        return {**out, "status": "HELD"}
    loan = ndc["loan"]
    receipt = _loan_receipt(loan, on_file) if loan else None
    loan_without = loan if loan and receipt is None else Decimal(0)
    # The loan's receipt a little under the NDC's figure: the lender's charges, not a receipt missing.
    charges = max(loan - receipt, Decimal(0)) if receipt is not None else Decimal(0)
    short = ndc["money"] - loan_without - charges - on_file["total"]
    return {**out, "loanWithoutReceipt": loan_without, "lenderCharges": charges,
            "short": short if short > RECEIPTS_TOLERANCE else Decimal(0),
            "status": "SHORT" if short > RECEIPTS_TOLERANCE else "COVERED"}


def missing_receipts_total(connection: Connection, *, tenant_id: str, journey_id: UUID) -> Decimal:
    """The payment receipts not on file, the loan apart (0 unless SHORT). Never
    money received: nothing here is counted as paid or listed among the
    payment receipts (product owner 2026-11-17)."""
    return receipts_against_ndc(connection, tenant_id=tenant_id, journey_id=journey_id)["short"]


def loan_without_receipt(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """The loan the NDC shows received with no payment receipt on file for it
    (product owner 2026-11-16: no task, a system remark)."""
    check = receipts_against_ndc(connection, tenant_id=tenant_id, journey_id=journey_id)
    if not check["loanWithoutReceipt"]:
        return []
    return [{"amount": check["loanWithoutReceipt"], "documentId": check.get("documentId"),
             "documentLabel": "NDC", "number": None, "date": None}]

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
    "RECEIPTS_TOLERANCE",
    "customer_ledger_balance",
    "is_loan_net_of_charges",
    "ledger_balance",
    "ledger_financier_credit",
    "ledger_loan_credit",
    "loan_without_receipt",
    "missing_receipts_total",
    "money_in",
    "ndc_money",
    "receipts_against_ndc",
]
