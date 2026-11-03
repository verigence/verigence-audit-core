"""Only billing documents count (product owner, 2026-11-02).

"We have 3-7 documents coming in: DMS invoice, Tally invoice, NDC, cost sheet,
account statement, accessories invoice, insurance, customer ledger. Any line
item read in these should be considered; additional documents help verify it.
No line item in the booking is confirmed: only things with a reference in the
above documents are confirmed, so no booking value is used to calculate a
variance."

1. A line no billing document carries is not current, whatever the booking
   docket offered; the booking stays on the sheet, as offered.
2. A figure from a document no longer on the booking (voided, replaced, moved)
   never shows.
3. Every invoice is read under the statement policy too: it stands where the
   NDC is silent, and the same benefit printed by both counts once.
4. A credit note is a billing document: counted once, its own reason naming
   the benefit.
5. The insurance premium counts only when confirmed in-house.
"""
from __future__ import annotations

from decimal import Decimal as D
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_evidence,
    add_ready_document,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text
from test_uc03_account_statement import UTKAL, _cost_sheet, _ledger, _set_policy, _sync

from audit_core.db import set_tenant_context
from audit_core.uc03_deal_source_policy import POLICY_ACCOUNT_STATEMENT

# A vehicle invoice billing 8,52,000 ex-showroom with an "OEM Discount" of
# 4,237.29 before GST (5,000.00 with it), no TCS printed.
OEM_DISCOUNT_INVOICE = {
    "invoice_purpose": "VEHICLE_SALE", "invoice_nature": "TAX_INVOICE", "invoice_number": "DMS/26/0901",
    "invoice_date": "2026-10-05", "gross_amount_before_discount": "722033.90", "taxable_amount": "717796.61",
    "cgst_rate": "9", "cgst_amount": "64601.69", "sgst_rate": "9", "sgst_amount": "64601.69",
    "grand_total_amount": "846999.99",
    "line_items": [
        {"line_category": "VEHICLE", "description_raw": "XUV 3XO AX5 PETROL MT", "gross_amount": 722033.90,
         "taxable_amount": 717796.61, "tax_rate": 18},
        {"line_category": "DISCOUNT_LINE", "description_raw": "OEM Discount", "taxable_amount": 4237.29},
    ],
}


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="bill")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _deal(journey) -> dict:
    from audit_core.uc03_p2_journey360 import deal

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return deal(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _row(view: dict, key: str) -> dict:
    return next(r for g in view["categories"] for r in g["components"] if r["key"] == key)


def _benefit(view: dict, key: str) -> dict:
    return next(d for d in view["discounts"] if d["key"] == key)


def _source(journey, key: str, document_type: str, amount: str, kind: str = "COMMERCIAL") -> None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, "
                       "component_key, source_document_type, amount, source_document_id) "
                       "VALUES (:t, :j, :k, :c, :d, :a, :doc)"),
                  {"t": journey.tenant_id, "j": journey.journey_id, "k": kind, "c": key, "d": document_type,
                   "a": amount, "doc": uuid4()})


def _canonical(journey, key: str, amount: str, reference: str) -> None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, actual_amount, "
                       "actual_source_kind, source_reference) VALUES (:t, :j, :k, :a, 'EVIDENCE', :r)"),
                  {"t": journey.tenant_id, "j": journey.journey_id, "k": key, "a": amount, "r": reference})


def _statement(journey, lines: list[dict]) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _ledger(journey, lines)
    _sync(journey)


# ── 1. the booking docket is never current ───────────────────────────────────
def test_a_line_only_the_booking_docket_offers_is_not_billed(journey) -> None:
    """Live 2026-11-02 (Bapu Dalabehera): the booking docket's misread
    "extended warranty 20,000" and "corporate discount 21,000" priced the
    deal. Under the NDC they are shown as offered and counted nowhere."""
    _statement(journey, UTKAL)
    before = _deal(journey)["summary"]["net"]["current"]
    _source(journey, "extended_warranty_amount", "booking_form", "20000")
    _source(journey, "CORPORATE_PRIVILEGE", "booking_form", "21000", kind="DISCOUNT")
    view = _deal(journey)
    warranty = _row(view, "extended_warranty_amount")
    assert D(warranty["booking"]) == D(20000) and warranty["current"] is None and warranty["variance"] is None
    corporate = _benefit(view, "CORPORATE_PRIVILEGE")
    assert D(corporate["booking"]) == D(21000) and corporate["current"] is None
    assert "NOT_ON_STATEMENT" in corporate["flags"]
    assert view["summary"]["net"]["current"] == before
    assert view["summary"]["variance"]["billedVsBooking"] is None


# ── 2. documents no longer on the booking ────────────────────────────────────
def test_a_figure_from_a_document_no_longer_on_file_never_shows(journey) -> None:
    """Live 2026-11-02 (D Hari Prasad): a replaced booking docket's misread
    TCS of 9,99,901 stood on the Deal from the canonical line."""
    gone, kept = uuid4(), uuid4()
    add_evidence(journey, di_document_id=gone, document_type_key="booking_docket", status="VOIDED")
    add_evidence(journey, di_document_id=kept, document_type_key="booking_docket")
    _canonical(journey, "tcs_amount", "999901", f"booking_form:{gone}")
    _canonical(journey, "ex_showroom_price", "969901", f"booking_form:{kept}")
    view = _deal(journey)
    tcs = _row(view, "tcs_amount")
    assert tcs["booking"] is None and tcs["effective"] is None and tcs["current"] is None
    assert D(_row(view, "ex_showroom_price")["booking"]) == D(969901)
    assert D(view["summary"]["gross"]["booking"]) == D(969901)


# ── 3. invoices under the statement policy ───────────────────────────────────
def test_the_invoice_stands_where_the_ndc_is_silent_and_one_benefit_counts_once(journey) -> None:
    """UTKAL gives "Other discount 5,000" and prints no ex-showroom figure the
    invoice disagrees with; the invoice's "OEM Discount" is the same 5,000:
    counted once, on the NDC's benefit."""
    _statement(journey, UTKAL)
    before = _deal(journey)
    add_ready_document(journey, "customer_invoice_dms", process_area="DELIVERY", **OEM_DISCOUNT_INVOICE)
    view = _deal(journey)
    given = _benefit(view, "ADDITIONAL_DISCOUNT")
    assert D(given["current"]) == D(5000)
    oem = _benefit(view, "CASH_DISCOUNT")
    assert oem["current"] is None and oem["sameAs"]["key"] == "ADDITIONAL_DISCOUNT"
    assert D(view["summary"]["discounts"]["current"]) == D(before["summary"]["discounts"]["current"])
    # The NDC's own vehicle cost still stands; the invoice checks it.
    assert D(_row(view, "ex_showroom_price")["billed"]) == D(1039000)


def test_an_invoice_discount_the_ndc_does_not_give_counts(journey) -> None:
    """Live 2026-11-02 (Biswadeep Jena, Bibhudatta Bhatta): the Tally invoice's
    "OEM Discount" (30,000 / 24,999 with GST) was dropped under the NDC policy."""
    _statement(journey, [line for line in UTKAL if line["particulars_raw"] != "Other Discount (if any)"])
    before = _deal(journey)["summary"]["net"]["current"]
    add_ready_document(journey, "customer_invoice_dms", process_area="DELIVERY", **OEM_DISCOUNT_INVOICE)
    view = _deal(journey)
    oem = _benefit(view, "CASH_DISCOUNT")
    assert D(oem["current"]) == D(5000) and oem["currentSource"] == "invoice"
    assert D(view["summary"]["net"]["current"]) == D(before) - 5000


# ── 4. credit notes ──────────────────────────────────────────────────────────
def test_a_credit_note_names_its_benefit_and_counts_once(journey) -> None:
    """The NDC prints the 5,000 as "Other discount"; the credit note for it
    says scrappage: one benefit, the scrappage bonus, counted once."""
    _statement(journey, UTKAL)
    before = _deal(journey)["summary"]["net"]["current"]
    add_ready_document(journey, "credit_note", process_area="DELIVERY", invoice_number="CN-9",
                       invoice_date="2026-10-20", grand_total_amount="5000", narration="SCRAPPAGE BONUS@40%")
    view = _deal(journey)
    scrappage = _benefit(view, "SCRAPPAGE_BONUS_DEALER")
    assert D(scrappage["current"]) == D(5000) and [c["number"] for c in scrappage["paidByCreditNotes"]] == ["CN-9"]
    other = _benefit(view, "ADDITIONAL_DISCOUNT")
    assert other["current"] is None and other["sameAs"]["key"] == "SCRAPPAGE_BONUS_DEALER"
    assert view["summary"]["net"]["current"] == before


# ── 5. insurance ─────────────────────────────────────────────────────────────
def test_the_premium_counts_only_when_confirmed_in_house(journey) -> None:
    """Live 2026-11-02 (Balabhadra Dash): the customer's own policy, Rs 66,643,
    no dealer code, no premium on any dealer document, made the settlement
    short by that much."""
    _source(journey, "ex_showroom_price", "tax_invoice_tally", "1000000")
    add_ready_document(journey, "insurance_cover", process_area="DELIVERY", premium_amount="66643",
                       insurer_name="ACME GENERAL", policy_number="P-1")
    _source(journey, "insurance_amount", "insurance_cover", "66643")
    view = _deal(journey)
    assert view["insurance"]["inDeal"] is False
    assert D(view["summary"]["net"]["current"]) == D(1000000)
    # A dealer bill charging the premium confirms it.
    _source(journey, "insurance_amount", "tax_invoice_tally", "66643")
    view = _deal(journey)
    assert view["insurance"]["inDeal"] is True
    assert D(view["summary"]["net"]["current"]) == D(1066643)


def test_an_invoice_discount_the_ndc_does_not_show_is_a_check_not_an_addition(journey) -> None:
    """The NDC gives the deal's benefits (5,000 here); the invoice prints a
    discount of another amount: one discount split differently, not a second
    one. It is flagged, never added beside the NDC's."""
    _statement(journey, UTKAL)
    before = _deal(journey)
    invoice = {**OEM_DISCOUNT_INVOICE, "taxable_amount": "716102.54", "cgst_amount": "64449.23",
               "sgst_amount": "64449.23", "grand_total_amount": "845001.00",
               "line_items": [OEM_DISCOUNT_INVOICE["line_items"][0],
                              {"line_category": "DISCOUNT_LINE", "description_raw": "OEM Discount",
                               "taxable_amount": 5931.36}]}
    add_ready_document(journey, "customer_invoice_dms", process_area="DELIVERY", **invoice)
    view = _deal(journey)
    oem = _benefit(view, "CASH_DISCOUNT")
    assert oem["current"] is None and "INVOICES_DISAGREE" in oem["flags"]
    assert D(view["summary"]["discounts"]["current"]) == D(before["summary"]["discounts"]["current"])


def test_an_invoice_discount_already_in_the_statement_price_is_not_given_twice(journey) -> None:
    """Live 2026-11-02 (Chinmay Kumar Gajendra): the cost sheet prices the
    vehicle at the invoice's price less the invoice's discount."""
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _cost_sheet(journey, {"ex_showroom_price": "847000", "accessories_amount": "10000"})
    _sync(journey)
    add_ready_document(journey, "customer_invoice_dms", process_area="DELIVERY", **OEM_DISCOUNT_INVOICE)
    view = _deal(journey)
    oem = _benefit(view, "CASH_DISCOUNT")
    assert oem["current"] is None and oem["inPrice"] is True
    vehicle = _row(view, "ex_showroom_price")
    assert D(vehicle["billed"]) == D(847000) and "INVOICES_DISAGREE" not in vehicle["flags"]
