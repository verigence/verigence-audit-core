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
from audit_core.uc03_p2_journey360 import _column_for, _dec

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


def entitlement_unknown(row: dict[str, Any]) -> bool:
    """The entitlement is not known (master not mapped, deal not reconciled,
    corporate category to be confirmed): not an over-grant, its own task says
    why (2026-10-20, review of PR 472)."""
    return row.get("entitled") is None and row.get("eligibility") in ENTITLEMENT_UNKNOWN_RESULTS


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
    whole amount given is the over-grant."""
    return _dec(row.get("entitled")) or Decimal(0)


def is_over(given_amount: Decimal, entitled_amount: Decimal) -> bool:
    return given_amount - entitled_amount > LINE_TOLERANCE


def given_document(row: dict[str, Any], column: str | None) -> str | None:
    """The document the given figure came from, as the Deal names it ("Tax
    invoice", "NDC (Account Statement)", "Cost sheet (no NDC on file)",
    "Booking docket"), so the reader of the tab sees what each figure rests on."""
    if column == "effective":
        return "Discount reconciliation"
    if column is None:
        return None
    amount = _dec(row.get(column))
    entries = [s for s in row.get("sources") or [] if _column_for(s.get("documentType")) == column]
    same = [s for s in entries if _dec(s.get("amount")) == amount]
    pick = (same or entries or [None])[0]
    if pick is not None:
        return pick.get("document")
    return "Invoice" if column == "billed" and row.get("billedFrom") == "invoice" else None


def standards_known(view: dict[str, Any]) -> bool:
    """Some component of the Deal has a standard price: the model was resolved
    against a published price list."""
    return any(c.get("standard") is not None for g in view.get("categories") or [] for c in g.get("components") or [])


__all__ = [
    "GIVEN_COLUMNS",
    "LINE_TOLERANCE",
    "entitled",
    "entitlement_unknown",
    "given",
    "given_document",
    "has_figure",
    "is_over",
    "standards_known",
]
