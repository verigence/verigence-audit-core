"""uc03_discount_given.py -- what a Deal's discount line says was given, and
whether that is more than the entitlement.

One resolution, read by both the EXCESS_DISCOUNT rule
(``uc03_p2_audit_rules.excess_discount``) and the Excess Discount tab
(``uc03_discount_variance.discount_variance_rows``), so the tab lists exactly
the discounts the rule raises its "Discount given beyond the entitlement" task
on (product owner 2026-10-31: "Excess Discount tab is empty dont see any
discount given more" -- the rule had 25 open tasks in Mahindra Orissa and the
tab showed none, because the tab read only a figure taken from an invoice and
that Project's Deal takes its figures from the NDC / account statement).

Every function reads one discount row of ``uc03_p2_journey360.deal`` as it is
built (``view["discounts"]``), never the screen copy.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from audit_core.uc03_masters_alignment import ENTITLEMENT_UNKNOWN_RESULTS
from audit_core.uc03_p2_journey360 import (
    _DEALER_BOOKS_NORM,
    _STATEMENT_NORM,
    _column_for,
    _dec,
    _norm_type,
)

# Per discount line: a gap at or below this is rounding, not an over-grant.
LINE_TOLERANCE = Decimal(100)

# The Deal column the given figure is read from: what the billing document
# gave (the invoice, or for an ACCOUNT_STATEMENT Project the NDC or the cost
# sheet standing in for it). Only billing documents count (product owner
# 2026-11-02): what the booking docket offered, and the discount application's
# reconciled amount written from it, are never a discount given (2026-11-03).
GIVEN_COLUMNS = ("billed",)


def counted(row: dict[str, Any]) -> bool:
    """The Deal counts the line (2026-11-03). A billed figure the Deal set
    aside is no discount given: the same benefit as another line
    (``sameAs``), a discount already inside the statement's price
    (``inPrice``), or an invoice discount the NDC / cost sheet does not give
    (``NOT_ON_STATEMENT``, ``INVOICES_DISAGREE``) -- the Deal's ``current`` is
    then empty. Live, SHI-SS-261031-004: the invoice's Rs 30,000.60 the NDC
    does not give, counted out of the Deal, was raised as an excess discount."""
    return row.get("current") is not None and not row.get("sameAs") and not row.get("inPrice")


def has_figure(row: dict[str, Any]) -> bool:
    """A line the Deal counts with a figure a billing document gives. The
    Management Referral line is always on the sheet, opted out by default:
    with no figure it is no discount yet."""
    return counted(row) and any(row.get(c) is not None for c in GIVEN_COLUMNS)


# Lines with no master to compare against (product owner 2026-11-06, the
# re-evaluation of the Excess Discount list): reported as "entitlement
# unknown", never as an over-grant of the whole amount.
UNTYPED_KEYS = frozenset({"UNCLASSIFIED_DISCOUNT", "COST_SHEET_DISCOUNT", "CREDIT_NOTE"})
OLD_VEHICLE_KEYS = frozenset({"EXCHANGE_BONUS", "WELCOME_BONUS", "SCRAPPAGE_BONUS_DEALER", "SCRAPPAGE_BONUS_COD"})
DEALER_DISCOUNT_KEY = "ADDITIONAL_DISCOUNT"
CASH_KEY = "CASH_DISCOUNT"
KIT_KEY = "ACCESSORIES_KIT"
_NOT_DECIDED = frozenset({"MANAGEMENT_REFERRAL"})  # its entitlement is the Team Lead's approval, on the row


def _zero_or_none(value: Any) -> bool:
    amount = _dec(value)
    return amount is None or amount == 0


def _proven(row: dict[str, Any]) -> bool:
    return row.get("evidenceStatus") == "VERIFIED" or bool((row.get("proof") or {}).get("onFile"))


def unknown_reason(row: dict[str, Any]) -> str | None:
    """Why the entitlement of this line is not known, or None when it is.

    * the reconciliation says so (master not mapped -- an empty published
      scheme version included --, deal not reconciled, corporate category to
      be confirmed, old vehicle not verified; 2026-10-20, review of PR 472);
    * the line has no reconciliation row at all: nothing compared it with a
      master (2026-11-06);
    * "Other scheme" with no scheme behind it (2026-11-06);
    * a discount whose type is not printed ("Invoice discount (type not
      printed)", the cost sheet's untyped total, an unnamed credit note):
      no master can be its entitlement (2026-11-06);
    * an old-vehicle benefit (exchange, welcome, scrappage) with no scheme
      behind it and the old vehicle not proven (no RC / COD read): not
      verified, not "none" (2026-11-06)."""
    key = str(row.get("key") or "")
    eligibility = row.get("eligibility")
    entitled_amount = row.get("entitled")
    if key in _NOT_DECIDED:
        return None
    if entitled_amount is None and eligibility in ENTITLEMENT_UNKNOWN_RESULTS:
        return {"MASTER_UNMATCHED": "the scheme master is not matched to this vehicle",
                "OUT_OF_GRID_SCOPE": "the dealer discount grid puts this model outside the discount program",
                "NOT_EVALUATED": "the deal is not reconciled against the masters yet",
                "SCHEME_NOT_IN_FORCE": "no scheme in force on the pricing date covers this vehicle",
                "NEEDS_CORPORATE_EVIDENCE": "the corporate category is still to be confirmed",
                "OLD_VEHICLE_UNVERIFIED": "the old vehicle is not verified (no RC / valuation report read)",
                "LOYALTY_MAY_BE_COD": "may be the COD scrappage bonus"}.get(str(eligibility),
                                                                               "the entitlement is not known")
    if key in UNTYPED_KEYS:
        return "the discount's type is not printed, so no scheme can be its entitlement"
    if eligibility is None and (entitled_amount is None or (key == DEALER_DISCOUNT_KEY and _zero_or_none(entitled_amount))):
        return "no reconciliation against the masters for this line"
    if key == "OTHER_SCHEME" and not row.get("scheme") and _zero_or_none(entitled_amount):
        return "no scheme master stands behind \"Other scheme\""
    if key in OLD_VEHICLE_KEYS and not row.get("scheme") and _zero_or_none(entitled_amount) and not _proven(row):
        return "the old vehicle is not verified (no RC / certificate read)"
    if (key in OLD_VEHICLE_KEYS and eligibility == "ELIGIBLE_UNCLAIMED" and _zero_or_none(entitled_amount)
            and row.get("potential") is not None and not _proven(row)):
        return "the old vehicle is not verified (no RC / certificate read)"
    return None


def entitlement_unknown(row: dict[str, Any]) -> bool:
    """The entitlement is not known: not an over-grant, its own task says why
    (2026-10-20, review of PR 472; widened 2026-11-06, ``unknown_reason``)."""
    return unknown_reason(row) is not None


def given(row: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    """The amount given on this line and the Deal column it was read from."""
    if not counted(row):
        return None, None
    for column in GIVEN_COLUMNS:
        if row.get(column) is not None:
            return _dec(row[column]), column
    return None, None


def entitled(row: dict[str, Any]) -> Decimal:
    """What the deal was entitled to on this line. With no entitlement on
    record (and it not "unknown", see above) the entitlement is zero: the
    whole amount given is the over-grant.

    An eligible benefit the reconciliation saw as unclaimed -- it reads its
    own fields, while the Deal took the figure from another document (the
    customer ledger's credit note naming the NDC's row, the cost sheet's total
    named by the invoice) -- keeps the amount the master gives it (the
    scheme's amount, the dealer grid's buffer) as its entitlement, not zero
    (2026-11-06)."""
    amount = _dec(row.get("entitled"))
    if (not amount and row.get("eligibility") == "ELIGIBLE_UNCLAIMED" and row.get("potential") is not None
            and (row.get("key") not in OLD_VEHICLE_KEYS or _proven(row))):
        return _dec(row["potential"]) or Decimal(0)
    return amount or Decimal(0)


def is_over(given_amount: Decimal, entitled_amount: Decimal) -> bool:
    return given_amount - entitled_amount > LINE_TOLERANCE


def _statement_first(source: dict[str, Any]) -> int:
    kind = _norm_type(source.get("documentType"))
    return 0 if kind in _STATEMENT_NORM else 1 if kind in _DEALER_BOOKS_NORM else 2


def given_document(row: dict[str, Any], column: str | None) -> str | None:
    """The document the given figure came from, as the Deal names it ("Tax
    invoice", "NDC (Account Statement)", "Cost sheet (no NDC on file)",
    "Booking docket"), so the reader of the tab sees what each figure rests on.

    The dealer's statement (NDC, the cost sheet standing in for it, the ERP
    settlement) is named first when it gives the figure, then the customer
    ledger, then an invoice: the Deal counts a discount from the statement
    only, and an invoice or ledger line with the same amount merely names it
    (2026-11-06: rows read "Tax Invoice (Tally)" for the NDC's figure). A
    benefit the statement gives as part of a total another document names
    (``fromStatement``) is the statement's."""
    if column == "effective":
        return "Discount reconciliation"
    if column is None:
        return None
    amount = _dec(row.get(column))
    entries = sorted((s for s in row.get("sources") or [] if _column_for(s.get("documentType")) == column),
                     key=_statement_first)
    if row.get("fromStatement"):
        statement = [s for s in entries if _norm_type(s.get("documentType")) in _STATEMENT_NORM]
        if statement:
            return statement[0].get("document")
    same = [s for s in entries if _dec(s.get("amount")) == amount]
    pick = (same or entries or [None])[0]
    if pick is not None:
        return pick.get("document")
    return "Invoice" if column == "billed" and row.get("billedFrom") == "invoice" else None


def standards_known(view: dict[str, Any]) -> bool:
    """Some component of the Deal has a standard price: the model was resolved
    against a published price list."""
    return any(c.get("standard") is not None for g in view.get("categories") or [] for c in g.get("components") or [])


# Statuses of a discount line (the tab's and the rules' one reading).
OVER, UNDER, UNKNOWN, KIT_AS_CASH = "OVER_ENTITLEMENT", "UNDER_ENTITLEMENT", "ENTITLEMENT_UNKNOWN", "KIT_AS_CASH"


def kit_available(discounts: list[dict[str, Any]]) -> Decimal:
    """The accessories kit the deal is entitled to and no accessories invoice
    used: the consumer scheme gives it in kind (Scorpio Classic, Thar Roxx),
    and a dealer who passes it on as cash on the consumer offer has given it
    as cash (product owner 2026-11-06: a breach, listed apart, not excess)."""
    kit = next((r for r in discounts if r.get("key") == KIT_KEY), None)
    if kit is None or has_figure(kit) or kit.get("eligibility") not in ("ELIGIBLE", "ELIGIBLE_UNCLAIMED"):
        return Decimal(0)
    amount = _dec(kit.get("entitled"))
    if not amount and kit.get("potential") is not None:
        amount = _dec(kit["potential"])
    return amount if amount and amount > 0 else Decimal(0)


def assess(discounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every discount line the Deal counts, judged per line (product owner
    2026-11-06: the basis stays per line), in the Deal's order:

    * ENTITLEMENT_UNKNOWN: no master to compare with (``unknown_reason``);
    * KIT_AS_CASH: the part of the consumer offer the unused accessories kit
      covers -- reported with the kit amount, never as excess;
    * OVER_ENTITLEMENT: given above the entitlement (the kit, when it was
      given as cash, counted with it) by more than the line tolerance;
    * UNDER_ENTITLEMENT: given below it by more than the tolerance;
    * WITHIN: within the tolerance.

    Each item: row, given, column, status, entitled (with the kit where it
    counted), variance (the excess, the kit part, or the shortfall), kit, reason."""
    kit_left = kit_available(discounts)
    out: list[dict[str, Any]] = []
    for row in discounts:
        if not has_figure(row):
            continue
        amount, column = given(row)
        if amount is None:
            continue
        base = {"row": row, "given": amount, "column": column, "kit": None, "reason": None}
        reason = unknown_reason(row)
        if reason is not None:
            out.append({**base, "status": UNKNOWN, "entitled": None, "variance": None, "reason": reason})
            continue
        entitled_amount = entitled(row)
        extra = amount - entitled_amount
        if row.get("key") == CASH_KEY and extra > LINE_TOLERANCE and kit_left > 0:
            kit_used = min(extra, kit_left)
            kit_left -= kit_used
            out.append({**base, "status": KIT_AS_CASH, "entitled": entitled_amount, "variance": kit_used,
                        "kit": kit_used, "reason": "accessories kit given as cash"})
            entitled_amount += kit_used
            extra -= kit_used
            if extra <= LINE_TOLERANCE:
                continue
        if extra > LINE_TOLERANCE:
            status = OVER
        elif -extra > LINE_TOLERANCE:
            status = UNDER
        else:
            status = "WITHIN"
        out.append({**base, "status": status, "entitled": entitled_amount, "variance": extra})
    return out


__all__ = [
    "GIVEN_COLUMNS",
    "KIT_AS_CASH",
    "LINE_TOLERANCE",
    "OVER",
    "UNDER",
    "UNKNOWN",
    "assess",
    "entitled",
    "entitlement_unknown",
    "given",
    "given_document",
    "has_figure",
    "is_over",
    "kit_available",
    "standards_known",
    "unknown_reason",
]
