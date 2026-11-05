"""Audit summary of one Journey: every financial fact on one page, each with a
conclusion (product owner 2026-10-29).

Built on demand, when the tab is opened (Journey 360 section
``audit-summary``), from the figures the other Journey 360 tabs already show --
the Deal, Payments, Add-ons and Trade-in builders -- so the summary never
disagrees with the tab that holds the detail. Nothing here reads a document
afresh, guesses a value or stores anything: a figure that is not on file is
shown as not on file, and the row says what is missing.

Every conclusion comes from a fixed rule (``rule`` on each row, see RULES)
with one tolerance: a difference of up to Rs 10 either way is no difference.

What the customer owes (the Deal lines), the benefits given, the products sold,
how the deal was paid, and where the documents disagree; then one verdict for
the Journey:

    CLEAN       every row concludes, and none is an exception
    EXCEPTIONS  at least one row is an exception, unsupported or disputed
    INCOMPLETE  no exception, but some rows cannot be concluded yet (the
                verdict says what is missing)

The billed figure of a line is the Deal's own; where the Deal has none, the
first document that prints the line is taken, in the order account statement
(NDC), cost sheet, customer ledger, invoice (product owner 2026-10-29), and the
row names that document.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

TOLERANCE = Decimal(10)
_TCS_TOLERANCE = Decimal(100)

# Conclusions, as the screen shows them.
OK = "OK"                      # agrees, within Rs 10
EXCEPTION = "EXCEPTION"        # differs by more than Rs 10 (who loses: impact)
UNSUPPORTED = "UNSUPPORTED"    # charged or given with no document that proves it
DISPUTED = "DISPUTED"          # two documents print different figures
CANNOT = "CANNOT_DETERMINE"    # a figure needed for the comparison is not on file
INFO = "INFO"                  # a fact, nothing to conclude (not taken, eligible but not given)

CUSTOMER = "CUSTOMER"          # the customer paid more than they should have
COMPANY = "COMPANY"            # the dealership gave more than it should have

RULES = {
    "AS-01": "Charged against the standard figure (price master, or the statutory rate for TCS)",
    "AS-02": "Charged against its own proof document (RTO challan, insurance policy)",
    "AS-03": "Documents that print the same line agree",
    "AS-04": "Benefit given against its entitlement",
    "AS-05": "Benefit that needs a proof document has it on file",
    "AS-06": "Product sold has a document that proves it",
    "AS-07": "Loan disbursed against the loan sanctioned",
    "AS-08": "Customer payments verified against a bank statement",
    "AS-09": "Money received against the net payable",
    "AS-10": "The deal's balance against the customer ledger's balance",
    "AS-11": "The NDC's and the cost sheet's rows add up to the totals they print",
}

# The document that proves a charge, by Deal line (third party: issued by the
# RTO or the insurer, not by the dealership).
_PROOF_OF_CHARGE: dict[str, tuple[str, ...]] = {
    "insurance_amount": ("insurance_cover", "insurance_policy", "insurance_cover_note"),
    "road_tax_registration": ("rto_challan", "rto_tax_receipt"),
    "road_tax_amount": ("rto_challan", "rto_tax_receipt"),
    "registration_charges": ("rto_challan", "rto_tax_receipt"),
}
# The document that proves a product was sold (the dealership's own invoice for it).
_PROOF_OF_PRODUCT: dict[str, tuple[str, ...]] = {
    "accessories_cost": ("accessory_invoice_dms", "accessory_invoice_tally"),
    "essential_kit_amount": ("accessory_invoice_dms", "accessory_invoice_tally"),
    "genuine_accessories_amount": ("accessory_invoice_dms", "accessory_invoice_tally"),
    "non_genuine_accessories_amount": ("accessory_invoice_dms", "accessory_invoice_tally"),
    "additional_warranty_amount": ("ew_invoice", "value_added_service_document"),
    "extended_warranty_amount": ("ew_invoice", "value_added_service_document"),
    "rsa_amount": ("rsa_invoice", "value_added_service_document"),
    "fastag_amount": ("value_added_service_document",),
}
# Which document the billed figure is taken from when the Deal has none.
# The dealer's statement (as the Deal names it under the statement policy)
# comes first, so the insurance and RTO checks set the policy and the challan
# against the statement, never against themselves (2026-11-02).
_BILLED_ORDER = ("account_statement", "erp_settlement", "cost_sheet_in_place_of_ndc",
                 "customer_ledger", "cost_sheet", "customer_ledger_tally", "customer_invoice_dms", "tax_invoice_tally", "tax_invoice_dms",
                 "tax_invoice", "customer_invoice_dms_v2", "invoice_generic", "accessory_invoice_dms",
                 "accessory_invoice_tally", "ew_invoice", "rsa_invoice", "insurance_cover", "rto_challan",
                 "credit_note", "debit_note")
# Documents that print a billed figure (the booking form is the offer, not a bill).
_BILLING_DOCUMENTS = frozenset(_BILLED_ORDER)
# The dealership's own bills: distinct ones on one line add up. A credit note
# is never added in (2026-10-31).
_INVOICE_DOCUMENTS = frozenset({
    "customer_invoice_dms", "tax_invoice_tally", "tax_invoice_dms", "tax_invoice", "customer_invoice_dms_v2",
    "invoice_generic", "accessory_invoice_dms", "accessory_invoice_tally", "ew_invoice", "rsa_invoice",
})
# Lines whose figure is statutory or third-party: the price master's figure is
# an estimate, so they are measured against their proof document.
_STATUTORY = frozenset({"road_tax_registration", "road_tax_amount", "registration_charges", "insurance_amount"})
_VAS = ("insurance_amount", "additional_warranty_amount", "extended_warranty_amount", "rsa_amount", "fastag_amount",
        "accessories_cost", "essential_kit_amount", "genuine_accessories_amount", "non_genuine_accessories_amount",
        "service_package_amount")


def dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def money(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(Decimal("0.01")))


def _same(a: Decimal | None, b: Decimal | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= TOLERANCE


def _billing_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The documents that print a billed figure for the line, with distinct
    invoices counted on it added up into one entry (2026-10-31): two accessory
    invoices are two bills that together make the line, not two documents
    that disagree (the Deal adds them the same way)."""
    printed = [s for s in sources if s.get("documentType") in _BILLING_DOCUMENTS and dec(s.get("amount")) is not None
               and s.get("counted") is not False]
    bills = [s for s in printed if s.get("documentType") in _INVOICE_DOCUMENTS]
    if len(bills) < 2:
        return printed
    numbers = [str(s["invoiceNumber"]) for s in bills if s.get("invoiceNumber")]
    combined = {
        "document": f"{len(bills)} invoices" + (f" ({', '.join(numbers)})" if numbers else ""),
        "documentType": bills[0]["documentType"],
        "amount": money(sum((dec(s["amount"]) or Decimal(0) for s in bills), Decimal(0))),
        "invoices": [{"document": s.get("document"), "invoiceNumber": s.get("invoiceNumber"),
                      "amount": s.get("amount")} for s in bills],
    }
    return [s for s in printed if s not in bills] + [combined]


def billed_figure(row: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    """The Deal's billed figure, else the first document that prints the line
    (account statement, cost sheet, ledger, invoice), and where it is from."""
    billed = dec(row.get("billed"))
    if billed is not None:
        source = next((s.get("document") for s in _billing_sources(row.get("sources") or [])
                       if _same(dec(s.get("amount")), billed)), None)
        return billed, source or "Deal"
    candidates = sorted(_billing_sources(row.get("sources") or []),
                        key=lambda s: _BILLED_ORDER.index(s["documentType"]))
    if candidates:
        return dec(candidates[0]["amount"]), candidates[0].get("document")
    return None, None


# The GST rates a vehicle bill prints (GST 2.0: 18% and 40%; 5% and 28% on
# older or other bills). An invoice prints its discounts before GST, the NDC
# the amount given (independent check 2026-11-04: the consumer offer
# Rs 25,000 on the NDC is the invoice's Rs 21,186, 25,000 / 1.18).
_GST_RATES = (Decimal("0.05"), Decimal("0.18"), Decimal("0.28"), Decimal("0.40"))


def _one_figure(a: dict[str, Any], b: dict[str, Any], *, allowances: tuple[Decimal, ...], before_gst: bool) -> bool:
    """Two documents' figures for one line are the same figure: within Rs 10;
    or apart by exactly an allowance (a price net of a discount the deal
    counts); or, for a benefit, one the invoice's figure before GST."""
    x, y = dec(a["amount"]), dec(b["amount"])
    if x is None or y is None or abs(x - y) <= TOLERANCE:
        return True
    if any(abs(abs(x - y) - allowance) <= TOLERANCE for allowance in allowances):
        return True
    if before_gst:
        pairs = ((x, y, a), (y, x, b))
        return any(source.get("documentType") in _INVOICE_DOCUMENTS
                   and any(abs(low * (1 + rate) - high) <= TOLERANCE for rate in _GST_RATES)
                   for low, high, source in pairs)
    return False


def disagreement(row: dict[str, Any], *, allowances: tuple[Decimal, ...] = (),
                 before_gst: bool = False) -> list[dict[str, Any]]:
    """The documents that print this line, when they print different figures
    (more than Rs 10 apart, and not the same figure another way: see
    _one_figure); empty when they agree or only one prints it."""
    printed = _billing_sources(row.get("sources") or [])
    if len(printed) < 2 or all(_one_figure(a, b, allowances=allowances, before_gst=before_gst)
                               for i, a in enumerate(printed) for b in printed[i + 1:]):
        return []
    return [{"document": s.get("document"), "amount": money(dec(s["amount"]))} for s in printed]


def _row(section: str, key: str, label: str, **fields: Any) -> dict[str, Any]:
    out = {"section": section, "key": key, "label": label, "entitled": None, "booked": None, "billed": None,
           "billedFrom": None, "proof": None, "difference": None, "conclusion": INFO, "impact": None,
           "reason": None, "rule": None, "documents": []}
    out.update(fields)
    return out


def registration_row(folded: dict[str, Any], parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Road tax + registration as one line (the Deal folds them, see
    uc03_p2_journey360._fold_registration): per document, the whole RTO amount
    it prints -- its combined figure, else its road tax and registration added
    (the RTO challan splits them; a cost sheet or statement prints one figure)."""
    part_sources = [s for part in parts for s in part.get("sources") or []]
    combined = [s for s in folded.get("sources") or [] if s not in part_sources]
    totals: dict[str, dict[str, Any]] = {}
    for s in combined:
        if dec(s.get("amount")) is not None and s.get("documentType") not in totals:
            totals[s["documentType"]] = dict(s)
    for s in part_sources:
        kind, amount = s.get("documentType"), dec(s.get("amount"))
        if amount is None or s.get("counted") is False:
            continue
        if kind in totals and not totals[kind].get("_parts"):
            continue  # this document printed the whole amount already
        entry = totals.setdefault(kind, {**s, "amount": "0", "_parts": True})
        entry["amount"] = str(dec(entry["amount"]) + amount)  # type: ignore[operator]
    sources = [{k: v for k, v in entry.items() if k != "_parts"} for entry in totals.values()]
    return {**folded, "sources": sources}


def conclude_charge(row: dict[str, Any], *, on_file: set[str],
                    discounts: tuple[Decimal, ...] = ()) -> dict[str, Any]:
    """One Deal line (a charge): billed against the price master, or against
    its own proof document for a statutory / insurance line. ``discounts``
    are the amounts the deal counts off the vehicle: an ex-showroom printed
    net of one of them (or of all) is the same price (independent check
    2026-11-04: the NDC's price net of the dealer discount was disputed
    against the invoice's gross price, 4 journeys)."""
    key = str(row["key"])
    billed, billed_from = billed_figure(row)
    standard = dec(row.get("standard"))
    disputed = disagreement(row, allowances=discounts if key == "ex_showroom_price" else ())
    proof_types = _PROOF_OF_CHARGE.get(key, ())
    proof_amount = next((dec(s["amount"]) for s in row.get("sources") or []
                         if s.get("documentType") in proof_types and dec(s.get("amount")) is not None), None)
    out = _row("CHARGE", key, str(row.get("label") or key), entitled=money(standard),
               booked=money(dec(row.get("booking"))), billed=money(billed), billedFrom=billed_from,
               documents=disputed)
    if proof_types:
        out["proof"] = {"documents": list(proof_types), "onFile": any(t in on_file for t in proof_types),
                        "amount": money(proof_amount)}
    if billed is None and key == "tcs_amount" and standard is not None and standard > 0:
        # TCS not charged on a car that owes it is TCS short, not undetermined
        # (product owner 2026-11-04; independent check: CANNOT_DETERMINE shown).
        billed = Decimal(0)
        out.update(billed=money(billed), billedFrom="no bill charges it")
    if billed is None:
        out.update(conclusion=CANNOT, reason="Not billed on any document on file.")
        return out
    if disputed:
        out.update(conclusion=DISPUTED, rule="AS-03",
                   reason="The documents print different figures for this line.")
        return out
    if key in _STATUTORY:
        if proof_amount is None:
            out.update(conclusion=UNSUPPORTED if billed > 0 else OK, rule="AS-02",
                       reason="Charged, but no document from the RTO / insurer on file shows the amount."
                       if billed > 0 else None)
            return out
        diff = billed - proof_amount
        out["difference"] = money(diff)
        if abs(diff) <= TOLERANCE:
            out.update(conclusion=OK, rule="AS-02")
        else:
            out.update(conclusion=EXCEPTION, rule="AS-02", impact=CUSTOMER if diff > 0 else COMPANY,
                       reason="Charged more than the proof document shows." if diff > 0
                       else "Charged less than the proof document shows.")
        return out
    if standard is None:
        out.update(conclusion=CANNOT, rule="AS-01", reason="No price master figure for this line.")
        return out
    diff = billed - standard
    out["difference"] = money(diff)
    # TCS is off when more than Rs 100 from 1% (product owner 2026-11-04), the
    # tolerance of the TCS checks themselves
    if abs(diff) <= (_TCS_TOLERANCE if key == "tcs_amount" else TOLERANCE):
        out.update(conclusion=OK, rule="AS-01")
    else:
        out.update(conclusion=EXCEPTION, rule="AS-01", impact=CUSTOMER if diff > 0 else COMPANY,
                   reason="Charged above the standard figure." if diff > 0 else "Charged below the standard figure.")
    return out


def conclude_benefit(row: dict[str, Any], *, unknown_results: frozenset[str]) -> dict[str, Any]:
    """One benefit (discount / offer): given against its entitlement, and its
    proof document when the benefit needs one."""
    key = str(row["key"])
    # Only what a billing document gives is given (product owner 2026-11-02):
    # the reconciled amount, written from the booking docket, is not.
    given, given_from = billed_figure(row)
    entitled = dec(row.get("entitled"))
    eligibility = row.get("eligibility")
    proof = row.get("proof")
    out = _row("BENEFIT", key, str(row.get("label") or key), entitled=money(entitled),
               booked=money(dec(row.get("booking"))), billed=money(given), billedFrom=given_from,
               documents=disagreement(row, before_gst=True), eligibility=eligibility,
               proof={"documents": [proof.get("documentType")], "onFile": bool(proof.get("onFile"))} if proof else None)
    # A figure the Deal does not count is never shown as given (2026-11-03,
    # SHI-SS-261027-024: the NDC's "Exchange / loyalty bonus" Rs 10,000 is the
    # ledger's scrappage credit note; the Deal counted it once, this page
    # listed it twice and asked for an exchange vehicle's RC).
    same_as = row.get("sameAs")
    if same_as:
        out.update(billed=None, billedFrom=None, documents=[], proof=None, conclusion=INFO, reason=(
            f"The same benefit as {same_as.get('label') or same_as.get('key')}: counted once, on that line."))
        return out
    if row.get("inPrice"):
        out.update(billed=None, billedFrom=None, documents=[], proof=None, conclusion=INFO, reason=(
            "Already in the vehicle price the dealer's statement charges: not given a second time."))
        return out
    if given and "current" in row and row["current"] is None:
        # The Deal does not count it (2026-11-04: ADI-BA-261019-003, an
        # invoice discount the NDC does not give, set aside with no flag,
        # was listed as given): the document's figure is shown, never as given.
        # Product owner 2026-11-04: discounts count from the NDC / cost sheet
        # only, so one printed elsewhere (invoice, credit note, Tally ledger)
        # is information, not a dispute.
        out.update(billed=None, billedFrom=None, conclusion=INFO,
                   documents=[{"document": given_from, "amount": money(given)}], reason=(
                       f"Printed on the {given_from or 'invoice'} only: discounts count from the NDC / "
                       "cost sheet, so the deal does not count it."))
        return out
    if not given:
        if entitled and eligibility == "ELIGIBLE":
            out.update(conclusion=INFO, rule="AS-04", reason="Entitled, not given.")
        else:
            out.update(conclusion=INFO, reason="Not given.")
        return out
    if out["documents"]:
        out.update(conclusion=DISPUTED, rule="AS-03", reason="The documents print different figures for this benefit.")
        return out
    if proof and not proof.get("onFile"):
        out.update(conclusion=UNSUPPORTED, rule="AS-05", impact=COMPANY,
                   reason=f"Given, but the {proof.get('document') or 'proof document'} is not on file.")
        return out
    if entitled is None or eligibility in unknown_results:
        out.update(conclusion=CANNOT, rule="AS-04",
                   reason="The entitlement is not known (" + str(eligibility or "no scheme or policy on record")
                   .replace("_", " ").lower() + ").")
        return out
    diff = given - entitled
    out["difference"] = money(diff)
    if abs(diff) <= TOLERANCE:
        out.update(conclusion=OK, rule="AS-04")
    else:
        out.update(conclusion=EXCEPTION, rule="AS-04", impact=COMPANY if diff > 0 else CUSTOMER,
                   reason="Given more than entitled." if diff > 0 else "Given less than entitled.")
    return out


def conclude_product(charge: dict[str, Any], *, on_file: set[str], taken: bool | None) -> dict[str, Any]:
    """A product sold (VAS): taken or not, and whether a document proves it."""
    out = dict(charge, section="PRODUCT")
    billed = dec(charge.get("billed"))
    proof_types = _PROOF_OF_PRODUCT.get(str(charge["key"]))
    if proof_types:
        out["proof"] = {"documents": list(proof_types), "onFile": any(t in on_file for t in proof_types),
                        "amount": None}
    out["taken"] = taken if taken is not None else (bool(billed) if billed is not None else None)
    if not billed:
        out.update(conclusion=INFO, rule=None, impact=None, reason="Not taken." if billed == 0 else out["reason"])
        return out
    if proof_types and not out["proof"]["onFile"] and out["conclusion"] in (OK, CANNOT):
        out.update(conclusion=UNSUPPORTED, rule="AS-06", impact=CUSTOMER,
                   reason="Charged, but no document that proves the product is on file.")
    return out


def loan_received(
    *, matched: Decimal | None, disbursement_known: bool, financed: bool, stated: dict[str, Any] | None,
    receipt_amounts: list[Decimal | None], letter: dict[str, Any] | None = None,
    loan_among_receipts: Decimal | None = None, payable: Decimal | None = None,
    paid_without_loan: Decimal | None = None,
) -> dict[str, Any]:
    """The loan money to count as received, and from where (2026-10-30).

    1. A disbursement on the finance record -- ``matched`` is the loan as
       uc03_p2_journey360.payments splits it (``loanDisbursed``): the receipt
       linked as the loan, which is then not among the customer's receipts,
       plus a disbursement not linked to a receipt (2026-11-02).
    2. Else the dealer's own statement: the NDC's financer line or the ERP
       settlement's finance DO posting -- unless the same amount is already
       among the customer's receipts (``receipt_amounts``): no receipt is
       linked as the loan yet (the PC is asked when the rules cannot tell
       which), so that receipt stays a customer receipt on Payments and
       here, and the loan is not counted a second time.
    3. Else the Bank Approval Letter's sanctioned amount, when no dealership
       receipt stands for the loan (2026-10-31; ``letter``, from
       uc03_p2_journey360.loan_without_receipt): counted, and marked
       ``withoutReceipt`` so the screen shows it with an asterisk. A customer
       receipt of the sanction less the lender's charges
       (``loan_among_receipts``, uc03_p2_journey360.letter_loan_among_receipts)
       is the loan's receipt not yet linked: the loan is in the receipts, not
       unknown and not counted twice (live 2026-11-03).
    4. Else, on a financed deal, the loan is NOT zero: it is unknown, and the
       settlement cannot be judged (REBARANI GANTHIA: the summary showed the
       whole loan as a shortfall).
    Refunds the settlement printout records are money paid back out."""
    refunds = (stated or {}).get("refunds") or Decimal(0)
    # ``inReceipts``: the loan is among the customer's receipts, not yet
    # linked as the loan's own (counted there, not again here).
    out: dict[str, Any] = {"amount": None, "source": None, "note": None, "unknown": False, "withoutReceipt": False,
                           "inReceipts": False,
                           "refunds": refunds, "refundsSource": (stated or {}).get("source") if refunds else None}
    if disbursement_known:
        out.update(amount=matched or Decimal(0), source="Loan disbursement on the finance record")
        return out
    loan = (stated or {}).get("loan")
    if loan is not None:
        if any(r is not None and abs(r - loan) <= TOLERANCE for r in receipt_amounts):
            out.update(amount=Decimal(0), source=stated["source"], inReceipts=True,
                       note="The loan on the statement is already among the receipts (not yet linked as the loan "
                                 "receipt); not counted twice.")
        else:
            out.update(amount=loan, source=stated["source"])
        return out
    letter_amount = dec((letter or {}).get("amount"))
    if letter_amount is not None:
        # The letter is the lender's approval, not money received. When the
        # money on file already pays all but less than half of it, the loan
        # is among the receipts (a disbursement whose reference does not name
        # the lender, or net of more than 2% charges): live 2026-11-03, a
        # Rs 23,20,189 "BOI" RTGS against a Rs 21,40,000 sanction, a Rs 6,84,200
        # Kotak credit against Rs 7,00,000 -- counted twice, read as excesses.
        if payable is not None and paid_without_loan is not None \
                and payable - paid_without_loan < letter_amount / 2:
            out.update(amount=Decimal(0), source="Bank Approval Letter (sanctioned amount)", inReceipts=True,
                       note=f"The money on file already pays the net payable but Rs {max(payable - paid_without_loan, Decimal(0)):,.2f}: "
                            f"the Bank Approval Letter's loan of Rs {letter_amount:,.2f} is taken to be among the receipts, "
                            "not counted again.")
            return out
        out.update(amount=letter_amount, source="Bank Approval Letter (sanctioned amount)", withoutReceipt=True,
                   note="No dealership receipt for the loan amount; taken from the Bank Approval Letter.")
        return out
    if loan_among_receipts is not None:
        out.update(amount=Decimal(0), source="Bank Approval Letter (sanctioned amount)", inReceipts=True,
                   note=f"The loan's receipt (Rs {loan_among_receipts:,.2f}, the sanction less the lender's charges) "
                        "is among the receipts, not yet linked as the loan receipt; not counted twice.")
        return out
    if financed:
        out.update(unknown=True, note="Financed deal: the loan amount is not on file (no disbursement on the "
                                      "finance record, no financer line on the NDC or settlement).")
    return out


def settlement(
    payable: Decimal | None, received: Decimal, *, loan_unknown: bool = False, awaiting: list[str] | None = None,
) -> dict[str, Any]:
    """Money received against the net payable. ``awaiting``: the lines still
    awaiting their billing document (product owner 2026-11-02: only billing
    documents count), so the payable is what is billed so far: a shortfall
    stands (the awaited bills can only add to it); an excess is not judged,
    as it may be paying for them."""
    if payable is None:
        return {"payable": None, "received": money(received), "balance": None, "status": None,
                "conclusion": CANNOT, "rule": "AS-09",
                "reason": "No net payable yet: the vehicle's price is not on a billing document "
                          "(NDC, cost sheet or vehicle invoice)."}
    if loan_unknown:
        # The lender's money is part of what was received; without it the
        # balance would be the loan itself, not a shortfall.
        return {"payable": money(payable), "received": money(received), "balance": None, "status": None,
                "conclusion": CANNOT, "rule": "AS-09", "impact": None,
                "reason": "Financed deal: the loan amount is not on file, so the settlement cannot be judged."}
    balance = payable - received
    status = "SETTLED" if abs(balance) <= TOLERANCE else ("SHORT" if balance > 0 else "EXCESS")
    if status == "EXCESS" and awaiting:
        return {"payable": money(payable), "received": money(received), "balance": money(balance), "status": None,
                "conclusion": CANNOT, "rule": "AS-09", "impact": None, "awaiting": list(awaiting),
                "reason": "Received more than is billed so far; still awaiting the bill for "
                          + ", ".join(awaiting) + ", which the difference may be paying for."}
    return {"payable": money(payable), "received": money(received), "balance": money(balance), "status": status,
            "conclusion": OK if status == "SETTLED" else EXCEPTION, "rule": "AS-09",
            # A balance is money still to come in (or to refund), not a price
            # or benefit given wrongly: it is not added to the amount at risk.
            "impact": None,
            "reason": None if status == "SETTLED" else (
                "Received less than the net payable." if status == "SHORT" else "Received more than the net payable.")}


# The ledger posts round-off and paise the deal does not carry: the product
# owner's price tolerance.
_LEDGER_TOLERANCE = Decimal(100)


def ledger_check(ledger: dict[str, Any] | None, settle: dict[str, Any]) -> dict[str, Any] | None:
    """The deal's balance (net payable less money received) set against the
    Tally customer ledger's (its debits less its credits): the ledger is the
    dealer's own record of every charge, benefit and payment, so the two agree
    within the tolerance or the difference is shown. None with no ledger."""
    if ledger is None:
        return None
    book = dec(ledger["balance"])
    shown = (f"Rs {abs(book):,.2f} {'due (Dr)' if book > 0 else 'paid over (Cr)' if book < 0 else 'nil'}")
    out = {"label": "Deal balance against the customer ledger", "rule": "AS-10", "impact": None,
           "ledgerBalance": money(book), "dealBalance": settle.get("balance"),
           "documentId": ledger.get("documentId")}
    deal_balance = dec(settle.get("balance"))
    if deal_balance is None:
        return {**out, "conclusion": CANNOT, "difference": None,
                "reason": f"The customer ledger shows {shown}; the deal's balance cannot be worked out yet."}
    difference = deal_balance - book
    if abs(difference) <= _LEDGER_TOLERANCE:
        return {**out, "conclusion": OK, "difference": money(difference), "reason": None}
    return {**out, "conclusion": EXCEPTION, "difference": money(difference),
            "reason": f"The customer ledger shows {shown}; the deal's balance is Rs {deal_balance:,.2f} "
                      f"({'due' if deal_balance > 0 else 'paid over'}): they differ by Rs {abs(difference):,.2f}."}


# The totals whose excess the customer pays (a charges or net total printed above
# its rows); a receipts total is the dealer's record of money in, no impact.
_CHARGED_TOTALS = frozenset({"Total Cost", "Gross Value", "Net Value", "On-road total"})


def statement_totals(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """AS-11 (product owner 2026-11-05): every NDC / cost-sheet copy whose rows do
    not give a total it prints is an exception on the summary. While the PC
    has not looked at it, it may still be a misreading (the PC's task,
    STATEMENT_ROWS_DO_NOT_ADD_UP, says so): no amount is put at risk. Once the
    PC closes that task saying the page was read right, it is the dealer's own
    arithmetic: the difference is the customer's (a charge or net total
    printed above its rows) or the dealership's (below)."""
    from audit_core.uc03_statement_totals import (
        TASK_TYPE,
        _rupees,
        copies_not_adding_up,
    )

    _, copies = copies_not_adding_up(connection, tenant_id=tenant_id, journey_id=journey_id)
    if not copies:
        return []
    task = connection.execute(
        text("SELECT completion_result FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
             "AND task_type=:k ORDER BY created_at_utc DESC LIMIT 1"),
        {"t": tenant_id, "j": journey_id, "k": TASK_TYPE},
    ).scalar_one_or_none() or {}
    confirmed = (task or {}).get("outcome") == "CLOSED_BY_PC"
    out = []
    for copy in copies:
        page = f" (page {copy['pageNumber']})" if copy["pageNumber"] else ""
        for d in copy["differences"]:
            difference = d["printed"] - d["rows"]
            impact = None
            if confirmed and d["total"] in _CHARGED_TOTALS:
                impact = CUSTOMER if difference > 0 else COMPANY
            reason = (f"{d['total']} printed {_rupees(d['printed'])}, its rows add up to {_rupees(d['rows'])}. "
                      + ("The PC confirmed the page was read right: the dealer's own sum is wrong"
                         + (f" ({(task or {}).get('reason')})" if (task or {}).get("reason") else "") + "."
                         if confirmed else "A row may be misread: the PC is checking the reading."))
            out.append({"label": f"{copy['document']}{page}: {d['total']}", "documentId": copy["documentId"],
                        "document": copy["document"], "pageNumber": copy["pageNumber"], "total": d["total"],
                        "printed": money(d["printed"]), "rows": money(d["rows"]), "difference": money(difference),
                        "conclusion": EXCEPTION, "rule": "AS-11", "impact": impact,
                        "dealerArithmetic": confirmed, "reason": reason})
    return out


def verdict(rows: list[dict[str, Any]], *, delivery_started: bool, missing: list[str]) -> dict[str, Any]:
    counts = {c: sum(1 for r in rows if r.get("conclusion") == c) for c in (OK, EXCEPTION, UNSUPPORTED, DISPUTED, CANNOT)}
    customer = sum((abs(dec(r.get("difference")) or Decimal(0)) for r in rows
                    if r.get("conclusion") == EXCEPTION and r.get("impact") == CUSTOMER), Decimal(0))
    company = sum((abs(dec(r.get("difference")) or Decimal(0)) for r in rows
                   if r.get("conclusion") == EXCEPTION and r.get("impact") == COMPANY), Decimal(0))
    if not delivery_started:
        status = "INCOMPLETE"
        missing = ["Delivery has not started: the deal is not final yet.", *missing]
    elif counts[EXCEPTION] or counts[UNSUPPORTED] or counts[DISPUTED]:
        status = "EXCEPTIONS"
    elif counts[CANNOT] or missing:
        status = "INCOMPLETE"
    else:
        status = "CLEAN"
    cannot = [f"{r['label']}: {r['reason']}" for r in rows if r.get("conclusion") == CANNOT and r.get("reason")]
    return {"status": status, "counts": counts,
            "amountAtRisk": {"customerOvercharged": money(customer), "companyLeakage": money(company)},
            "missing": missing + cannot}


# ── the Journey ──────────────────────────────────────────────────────────────
def _on_file(connection: Connection, *, tenant_id: str, journey_id: UUID) -> set[str]:
    return {str(t) for t in connection.execute(
        text("SELECT DISTINCT document_type_key FROM auditcore.evidence WHERE tenant_id=:t AND journey_id=:j "
             "AND association_status='ACTIVE' AND document_type_key IS NOT NULL"),
        {"t": tenant_id, "j": journey_id},
    ).scalars().all()}


def required_documents(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """The documents the file needs to be complete (product owner 2026-10-30):
    the same checklist the stage gates and the Missing tasks use -- mandatory
    documents, and conditional ones the deal's own evidence made mandatory
    (with why) -- for the booking and, once it has started, the delivery.
    Documents sharing a group are one line met by any of them."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_stage import (
        condition_reasons,
        delivery_has_started,
        requirement_items,
    )

    registry = get_registry()
    reasons = condition_reasons(connection, tenant_id=tenant_id, journey_id=journey_id)
    stages = ("BOOKING", "DELIVERY") if delivery_has_started(
        connection, tenant_id=tenant_id, journey_id=journey_id) else ("BOOKING",)
    out = []
    for stage in stages:
        for item in requirement_items(connection, registry, tenant_id=tenant_id, journey_id=journey_id,
                                      stage=stage, reasons=reasons):
            if not item["required"]:
                continue
            out.append({"key": item["key"], "label": item["label"], "stage": stage,
                        "requirement": "MANDATORY" if item["requirement"] == "REQUIRED" else "CONDITIONAL",
                        "reason": item["reason"], "onFile": bool(item["received"]),
                        "count": int(item["readyCount"] or 0)})
    # Missing first, then by stage as the deal runs.
    return sorted(out, key=lambda d: (d["onFile"], stages.index(d["stage"])))


def audit_summary(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    from audit_core import uc03_p2_journey360 as j360
    from audit_core.uc03_account_statement import statement_loan
    from audit_core.uc03_masters_alignment import ENTITLEMENT_UNKNOWN_RESULTS

    deal = j360.deal(connection, tenant_id=tenant_id, journey_id=journey_id)
    pays = j360.payments(connection, tenant_id=tenant_id, journey_id=journey_id)
    addons = j360.addons(connection, tenant_id=tenant_id, journey_id=journey_id)
    trade = j360.tradein(connection, tenant_id=tenant_id, journey_id=journey_id)
    on_file = _on_file(connection, tenant_id=tenant_id, journey_id=journey_id)
    summary = deal.get("summary") or {}
    delivery_started = bool(summary.get("deliveryStarted"))
    self_insured = (deal.get("insurance") or {}).get("source") == "SELF"
    in_deal = bool((deal.get("insurance") or {}).get("inDeal"))

    given_amounts = [amount for d in deal.get("discounts") or []
               if (amount := dec(d.get("current"))) is not None and amount > 0]
    discount_amounts = tuple(given_amounts) + ((sum(given_amounts, Decimal(0)),) if len(given_amounts) > 1 else ())
    charges, products = [], []
    for group in deal.get("categories") or []:
        lines = group.get("components") or []
        # Road tax and registration are one line; their parts are its breakdown.
        parts = [line for line in lines if line.get("partOf")]
        for line in lines:
            if line.get("excluded") or line.get("partOf"):
                continue
            if line["key"] == "road_tax_registration" and parts:
                line = registration_row(line, parts)
            row = conclude_charge(line, on_file=on_file, discounts=discount_amounts)
            row["category"] = group.get("label")
            if line["key"] in _VAS:
                taken = (line.get("opted") or {}).get("taken") if isinstance(line.get("opted"), dict) else None
                products.append(conclude_product(row, on_file=on_file, taken=taken))
            else:
                charges.append(row)
    benefits = [conclude_benefit(d, unknown_results=ENTITLEMENT_UNKNOWN_RESULTS) for d in deal.get("discounts") or []]

    # ── how the deal was paid ──
    # Every counted receipt is checked against the bank statement, the loan's
    # own receipt too; the customer's receipts and the loan are shown apart.
    counted = [i for i in pays.get("items") or [] if i.get("counted")]
    by_status: dict[str, Decimal] = {}
    for item in counted:
        status = (item.get("bankStatement") or {}).get("status") or "NO_STATEMENT"
        by_status[status] = by_status.get(status, Decimal(0)) + (dec(item.get("amount")) or Decimal(0))
    receipts = dec(pays.get("receiptsTotal")) or Decimal(0)
    counted_total = sum((dec(i.get("amount")) or Decimal(0) for i in counted), Decimal(0))
    records = (addons.get("finance") or {}).get("records") or []
    loan_on_file = loan_received(
        matched=dec(pays.get("loanDisbursed")),
        disbursement_known=any(dec(r.get("loan_disbursement_amount")) is not None for r in records),
        financed=bool(records),
        stated=statement_loan(connection, tenant_id=tenant_id, journey_id=journey_id),
        receipt_amounts=[dec(i.get("amount")) for i in counted if not i.get("isLoanReceipt")],
        letter=pays.get("loanWithoutReceipt"),
        loan_among_receipts=j360.letter_loan_among_receipts(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            receipt_amounts=[dec(i.get("amount")) for i in counted if not i.get("isLoanReceipt")]),
        payable=dec(summary.get("payable")),
        paid_without_loan=receipts + (dec((summary.get("paid") or {}).get("oldVehicle")) or Decimal(0)),
    )
    loan = loan_on_file["amount"] or Decimal(0)
    refunds = loan_on_file["refunds"]
    # The old vehicle the NDC takes against the price: paid in kind, as the
    # Deal counts it (2026-11-02, Deal defect 4).
    old_vehicle = dec((summary.get("paid") or {}).get("oldVehicle")) or Decimal(0)
    verified = by_status.get("MATCHED", Decimal(0))
    non_cash = counted_total - by_status.get("NOT_APPLICABLE", Decimal(0))
    bank_check = {
        "verified": money(verified), "cash": money(by_status.get("NOT_APPLICABLE", Decimal(0))),
        "notFound": money(by_status.get("UNMATCHED", Decimal(0)) + by_status.get("AMBIGUOUS", Decimal(0))),
        "noStatement": money(by_status.get("NO_STATEMENT", Decimal(0))),
        "rule": "AS-08",
        "conclusion": (OK if non_cash > 0 and _same(verified, non_cash) else
                       EXCEPTION if by_status.get("UNMATCHED") or by_status.get("AMBIGUOUS") else
                       CANNOT if non_cash > 0 else INFO),
        "reason": (None if non_cash > 0 and _same(verified, non_cash) else
                   "Some receipts have no matching credit on the bank statement."
                   if by_status.get("UNMATCHED") or by_status.get("AMBIGUOUS") else
                   "No bank statement on file to verify the receipts." if non_cash > 0 else None),
    }
    loans = []
    for record in records:
        sanctioned, disbursed = dec(record.get("sanctioned_amount")), dec(record.get("loan_disbursement_amount"))
        entry = {"lender": record.get("provider_name"), "sanctioned": money(sanctioned), "disbursed": money(disbursed),
                 "deliveryOrder": record.get("do_reference"), "rule": "AS-07"}
        if sanctioned is None or disbursed is None:
            entry.update(conclusion=CANNOT, reason="Sanctioned and disbursed amounts are not both on file.")
        elif _same(sanctioned, disbursed):
            entry.update(conclusion=OK, reason=None)
        else:
            entry.update(conclusion=EXCEPTION, difference=money(sanctioned - disbursed),
                         reason="Disbursed differs from sanctioned.")
        loans.append(entry)
    # Every credit note on file (2026-10-31: the statement-policy Deal reads no
    # invoices, so its invoice list never held one): shown, not settled against.
    credit_notes = deal.get("creditNotes") or []
    trade_in = trade.get("tradeIn") or {}
    funding = {
        "receipts": money(receipts), "loanDisbursed": money(loan_on_file["amount"]),
        "loanSource": loan_on_file["source"], "loanNote": loan_on_file["note"],
        "loanWithoutReceipt": loan_on_file["withoutReceipt"],
        "refunds": money(refunds) if refunds else None, "refundsSource": loan_on_file["refundsSource"],
        "oldVehicle": money(old_vehicle) if old_vehicle else None,
        "received": money(receipts + loan + old_vehicle - refunds),
        "bankCheck": bank_check, "loans": loans,
        "bankApprovalOnFile": "bank_approval_letter" in on_file, "deliveryOrderOnFile": "delivery_order_cover" in on_file,
        "exchange": {"applicable": (trade.get("exchange") or {}).get("applicable"),
                     "bookedValue": (trade.get("exchange") or {}).get("value"),
                     "quotedValue": money(dec(trade_in.get("quoted_value"))),
                     "actualValue": money(dec(trade_in.get("actual_value"))),
                     "valuationReports": len(trade.get("valuations") or [])},
        "scrappage": {"certificates": len(trade.get("certificates") or [])},
        "creditNotes": {"count": len(credit_notes),
                        "total": money(sum((dec(c.get("amount")) or Decimal(0) for c in credit_notes), Decimal(0))),
                        "items": credit_notes},
        "insurance": {"by": "Self (customer's own)" if self_insured
                      else "In-house (through the dealership)" if in_deal
                      else "Not confirmed: kept out of the deal until the PC confirms",
                      "decidedBy": (deal.get("insurance") or {}).get("decidedBy"),
                      "basis": (deal.get("insurance") or {}).get("basis")},
    }
    settle = settlement(dec(summary.get("payable")), receipts + loan + old_vehicle - refunds,
                        loan_unknown=loan_on_file["unknown"],
                        # Each awaited bill named once (two warranty lines are one warranty bill).
                        awaiting=list(dict.fromkeys(a.get("label") or a.get("key")
                                                    for a in summary.get("awaiting") or [])))

    from audit_core.uc03_ledger_receipts import customer_ledger_balance

    ledger = ledger_check(customer_ledger_balance(connection, tenant_id=tenant_id, journey_id=journey_id), settle)
    funding["customerLedger"] = ledger
    rows = charges + benefits + products + [dict(bank_check, label="Payments verified with bank statement",
                                                  difference=None, impact=None),
                                            *[dict(entry, label=f"Loan ({entry['lender'] or 'lender not named'})",
                                                   impact=None) for entry in loans],
                                            dict(settle, label="Money received against net payable",
                                                 difference=settle.get("balance")),
                                            *([ledger] if ledger else [])]
    totals = statement_totals(connection, tenant_id=tenant_id, journey_id=journey_id)
    rows += totals
    missing = []
    if not on_file & {"customer_invoice_dms", "tax_invoice_tally", "customer_ledger", "cost_sheet"}:
        missing.append("No invoice, account statement or cost sheet on file.")
    file_documents = required_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
    still_needed = [d for d in file_documents if not d["onFile"]]
    if still_needed:
        missing.append(f"{len(still_needed)} required document{'s' if len(still_needed) != 1 else ''} "
                       "not on file: " + ", ".join(d["label"] for d in still_needed) + ".")
    # A document on file that was not fully read (2026-11-04) leaves the audit
    # incomplete, said so, never concluded on what the other documents give.
    for unread in summary.get("statementNotRead") or []:
        missing.append(f"The NDC ({unread.get('title') or 'account statement'}) is on file but none of its "
                       "rows was read.")
    not_read = summary.get("figuresNotRead") or []
    if not_read:
        from audit_core.uc03_document_key_figures import describe

        missing.append("Figures not read from documents on file: " + describe(not_read) + ".")
    disputed = [r for r in charges + benefits + products if r["conclusion"] == DISPUTED]
    return {
        "generatedAtUtc": datetime.now(UTC).isoformat(),
        "tolerance": str(TOLERANCE),
        "rules": RULES,
        "billedSource": summary.get("billedSource"),
        "verdict": verdict(rows, delivery_started=delivery_started, missing=missing),
        # The deal as it is counted, so the header adds up to its net payable
        # (independent check 2026-11-04: the billed column also held invoice
        # discounts the deal does not count, beside a payable that leaves them out).
        "deal": {"charges": charges, "gross": (summary.get("gross") or {}).get("current"),
                 "discounts": (summary.get("discounts") or {}).get("current"),
                 "netPayable": summary.get("payable")},
        "benefits": benefits,
        "products": products,
        "funding": funding,
        "settlement": settle,
        "disputed": [{"label": r["label"], "documents": r["documents"]} for r in disputed],
        "requiredDocuments": file_documents,
        "documentsNotFullyRead": not_read,
        "statementTotals": totals,
    }
