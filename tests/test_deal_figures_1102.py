"""Deal figures under the statement policy (product owner, 2026-11-02).

1. TCS and ex-showroom come from the NDC / cost sheet first; only a line the
   statement does not print is taken from the vehicle invoice, named so.
2. The statement's one RTO figure is the whole amount: it stands on
   "Road tax + registration" and is set against the challan's parts added up.
3. Accessories, EW and RSA: the NDC's figure is what was charged; their
   invoices are what was invoiced, beside it. A line never awaits an invoice
   that is on file, and the vehicle's own lines await the NDC.
4. A credit note is linked to the one benefit of its amount.
5. What is payable is the deal's current net, never the price master.
"""
from __future__ import annotations

from decimal import Decimal as D
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import add_ready_document, create_p2_journey, database_engine
from sqlalchemy import text
from test_uc03_account_statement import (
    ADITYA_NO_VEHICLE,
    UTKAL,
    _ledger,
    _set_policy,
    _sync,
)

from audit_core.db import set_tenant_context
from audit_core.uc03_deal_source_policy import POLICY_ACCOUNT_STATEMENT

VEHICLE_INVOICE = {
    "invoice_purpose": "VEHICLE_SALE", "invoice_nature": "TAX_INVOICE", "invoice_number": "DMS/26/0815",
    "invoice_date": "2026-10-05", "vin_number": "MA1NE2XO3T4000001", "gross_amount_before_discount": "722033.90",
    "invoice_discount_amount": "21016.58", "taxable_amount": "701017.32", "cgst_rate": "9",
    "cgst_amount": "63091.56", "sgst_rate": "9", "sgst_amount": "63091.56", "tcs_amount": "8272.00",
    "grand_total_amount": "835472.44",
    "line_items": [
        {"line_category": "VEHICLE", "description_raw": "XUV 3XO AX5 PETROL MT", "gross_amount": 722033.90,
         "discount_amount": 21016.58, "taxable_amount": 701017.32, "tax_rate": 18, "net_amount": 827200.44},
    ],
}


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="fig")
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


def _source(journey, key: str, document_type: str, amount: str, kind: str = "COMMERCIAL") -> None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, "
                       "component_key, source_document_type, amount, source_document_id) "
                       "VALUES (:t, :j, :k, :c, :d, :a, :doc)"),
                  {"t": journey.tenant_id, "j": journey.journey_id, "k": kind, "c": key, "d": document_type,
                   "a": amount, "doc": uuid4()})


def _statement(journey, lines: list[dict]) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _ledger(journey, lines)
    _sync(journey)


# ── 1. TCS / ex-showroom ─────────────────────────────────────────────────────
def test_tcs_the_statement_does_not_print_comes_from_the_vehicle_invoice(journey) -> None:
    _statement(journey, [line for line in UTKAL if line["particulars_raw"] != "TCS"])
    add_ready_document(journey, "customer_invoice_dms", process_area="DELIVERY", **VEHICLE_INVOICE)
    view = _deal(journey)
    tcs = _row(view, "tcs_amount")
    assert D(tcs["billed"]) == D("8272.00")
    # The invoice is a billing document: it stands where the NDC is silent,
    # under its own name (product owner 2026-11-02).
    assert [s["documentType"] for s in tcs["sources"]] == ["customer_invoice_dms"]
    assert tcs["billedFrom"] == "invoice"
    # The statement still prices the vehicle; the invoice adds nothing else.
    assert D(_row(view, "ex_showroom_price")["billed"]) == D(1039000)
    assert view["summary"]["billedSource"] == "ACCOUNT_STATEMENT"


def test_the_statement_tcs_wins_over_the_invoice(journey) -> None:
    _statement(journey, UTKAL)  # prints TCS 10,390
    add_ready_document(journey, "customer_invoice_dms", process_area="DELIVERY", **VEHICLE_INVOICE)
    tcs = _row(_deal(journey), "tcs_amount")
    assert D(tcs["billed"]) == D(10390) and tcs["billedFrom"] == "document"


# ── 2. registration ──────────────────────────────────────────────────────────
def test_the_statement_rto_figure_is_the_whole_amount_against_the_challan_total(journey) -> None:
    _statement(journey, UTKAL)  # prints Registration 1,06,139
    _source(journey, "road_tax_amount", "rto_challan", "100000")
    _source(journey, "registration_charges", "rto_challan", "6139")
    row = _row(_deal(journey), "road_tax_registration")
    assert D(row["billed"]) == D(106139)
    assert "INVOICES_DISAGREE" not in row["flags"]


def test_a_challan_total_that_differs_from_the_statement_is_flagged(journey) -> None:
    _statement(journey, UTKAL)
    _source(journey, "road_tax_amount", "rto_challan", "100000")
    _source(journey, "registration_charges", "rto_challan", "5000")
    view = _deal(journey)
    row = _row(view, "road_tax_registration")
    assert D(row["billed"]) == D(106139) and "INVOICES_DISAGREE" in row["flags"]
    # The parts are its breakdown, never counted again.
    part = _row(view, "registration_charges")
    assert part.get("partOf") == "road_tax_registration" and D(part["billed"]) == D(5000)


# ── 3. add-ons ───────────────────────────────────────────────────────────────
def test_the_ew_invoice_stands_beside_the_statement_charge(journey) -> None:
    _statement(journey, UTKAL)  # Shield warranty 9,999
    add_ready_document(journey, "ew_invoice", process_area="DELIVERY", invoice_number="EW-1",
                       invoice_date="2026-10-05", grand_total_amount="9999")
    row = _row(_deal(journey), "additional_warranty_amount")
    assert D(row["billed"]) == D(9999) and row["billedFrom"] == "document"
    assert D(row["invoiced"]) == D(9999) and "INVOICES_DISAGREE" not in row["flags"]
    assert row["awaiting"] is None


def test_an_ew_invoice_that_differs_from_the_statement_is_flagged(journey) -> None:
    _statement(journey, UTKAL)
    add_ready_document(journey, "ew_invoice", process_area="DELIVERY", invoice_number="EW-1",
                       invoice_date="2026-10-05", grand_total_amount="8999")
    row = _row(_deal(journey), "additional_warranty_amount")
    assert D(row["billed"]) == D(9999) and "INVOICES_DISAGREE" in row["flags"]


def test_accessories_invoiced_above_the_charge_show_the_free_part(journey) -> None:
    _statement(journey, UTKAL)  # Accessories 30,000 charged
    add_ready_document(journey, "accessory_invoice_dms", process_area="DELIVERY", invoice_number="ACC-1",
                       invoice_date="2026-10-05", grand_total_amount="35000")
    row = _row(_deal(journey), "accessories_cost")
    assert D(row["billed"]) == D(30000) and D(row["invoiced"]) == D(35000)
    assert D(row["freeByInvoice"]) == D(5000)
    assert "INVOICES_DISAGREE" not in row["flags"]


def test_the_vehicle_line_awaits_the_ndc_not_a_vehicle_invoice(journey) -> None:
    _statement(journey, ADITYA_NO_VEHICLE)  # receipts and a cash discount, no vehicle line
    _source(journey, "ex_showroom_price", "booking_form", "1000000")
    row = _row(_deal(journey), "ex_showroom_price")
    assert row["awaiting"] == "NDC (Account Statement)"


# ── 4. credit notes ──────────────────────────────────────────────────────────
def test_a_credit_note_is_linked_to_the_benefit_of_its_amount_and_counted_once(journey) -> None:
    """2026-11-02: a credit note is a billing document. CN-1 pays the 5,000
    the NDC already gives: checked, not added. CN-2 names no benefit and
    matches none: a benefit of its own, counted."""
    _statement(journey, UTKAL)  # Other Discount (if any) 5,000
    add_ready_document(journey, "credit_note", process_area="DELIVERY", invoice_number="CN-1",
                       invoice_date="2026-10-20", grand_total_amount="5000")
    add_ready_document(journey, "credit_note", process_area="DELIVERY", invoice_number="CN-2",
                       invoice_date="2026-10-21", grand_total_amount="7777")
    view = _deal(journey)
    notes = {c["number"]: c for c in view["creditNotes"]}
    assert notes["CN-1"]["benefit"]["key"] == "ADDITIONAL_DISCOUNT"
    assert notes["CN-2"]["benefit"]["key"] == "CREDIT_NOTE" and notes["CN-2"]["benefitCandidates"] == []
    benefit = next(d for d in view["discounts"] if d["key"] == "ADDITIONAL_DISCOUNT")
    assert [c["number"] for c in benefit["paidByCreditNotes"]] == ["CN-1"] and D(benefit["current"]) == D(5000)
    unnamed = next(d for d in view["discounts"] if d["key"] == "CREDIT_NOTE")
    assert D(unnamed["current"]) == D(7777) and "BENEFIT_NOT_NAMED" in unnamed["flags"]


# ── 5. payable ───────────────────────────────────────────────────────────────
def test_nothing_is_payable_until_the_deal_has_a_figure(journey) -> None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, "
                       "standard_amount) VALUES (:t, :j, 'ex_showroom_price', 1000000)"),
                  {"t": journey.tenant_id, "j": journey.journey_id})
    summary = _deal(journey)["summary"]
    assert summary["payable"] is None and summary["balanceDue"] is None


# ── 6. insurance ─────────────────────────────────────────────────────────────
def test_an_insurance_premium_on_the_statement_makes_the_insurance_in_house(journey) -> None:
    from audit_core.uc03_insurance_source import resolve

    _statement(journey, UTKAL)  # Insurance amount 38,201
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        decided = resolve(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert decided["source"] == "INHOUSE" and decided["decidedBy"] == "DOCUMENT"
    assert "statement charges the insurance premium" in decided["basis"]


# ── 7. FASTag and 3. a benefit the NDC does not list ─────────────────────────
def test_fastag_is_billed_by_the_ndc_not_a_fastag_invoice(journey) -> None:
    _statement(journey, [line for line in UTKAL if line["particulars_raw"] != "Fastag charges"])
    _source(journey, "fastag_amount", "booking_form", "600")
    row = _row(_deal(journey), "fastag_amount")
    # the NDC is on file and does not charge it: not charged, never awaited
    assert row["awaiting"] is None
    assert (row["current"], row["currentSource"]) == ("0", "not_invoiced")


def test_fastag_awaits_the_ndc_while_the_ndc_has_no_vehicle_line(journey) -> None:
    _statement(journey, ADITYA_NO_VEHICLE)
    _source(journey, "fastag_amount", "booking_form", "600")
    assert _row(_deal(journey), "fastag_amount")["awaiting"].startswith("NDC (Account Statement)")


def test_a_benefit_the_booking_offered_that_the_ndc_does_not_list_is_flagged(journey) -> None:
    _statement(journey, UTKAL)  # lists only "Other Discount (if any)"
    _source(journey, "CORPORATE_PRIVILEGE", "booking_form", "10000", kind="DISCOUNT")
    view = _deal(journey)
    benefit = next(d for d in view["discounts"] if d["key"] == "CORPORATE_PRIVILEGE")
    assert benefit["current"] is None and "NOT_ON_STATEMENT" in benefit["flags"]


# ── 4 / 13. the old vehicle and the statement's printed totals ───────────────
def test_the_trade_in_value_is_the_old_vehicle_and_the_printed_totals_are_declared(journey) -> None:
    from test_uc03_account_statement import SHIVNATH

    from audit_core.uc03_account_statement import read_statement

    assert read_statement(SHIVNATH).old_vehicle_value == D(53155)
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    document_id, _ = _ledger(journey, SHIVNATH)
    from p2_support import add_extracted_field

    for key, value in {"net_value": "1900000", "total_receipts": "1964155", "balance_amount": "0"}.items():
        add_extracted_field(journey, di_document_id=document_id, field_key=key, value=value,
                            document_type="customer_ledger")
    _sync(journey)
    view = _deal(journey)
    declared = {d["key"]: d for d in view["declared"]}
    assert D(declared["old_vehicle_value"]["billed"]) == D(53155)
    # the old vehicle is paid in kind: in what is received, and off the balance
    paid = view["summary"]["paid"]
    assert D(paid["oldVehicle"]) == D(53155) and D(paid["total"]) == D(paid["receipts"]) + D(paid["loan"]) + D(53155)
    # the Audit Summary counts it the same way
    from audit_core.uc03_p2_audit_summary import audit_summary

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        funding = audit_summary(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)["funding"]
    assert D(funding["oldVehicle"]) == D(53155)
    assert D(funding["received"]) == D(funding["receipts"]) + D(funding["loanDisbursed"] or 0) + D(53155)
    assert D(declared["net_amount"]["billed"]) == D(1900000)
    assert D(declared["received_amount"]["billed"]) == D(1964155)
    assert D(declared["balance_amount"]["billed"]) == D(0)


# ── 5. one balance: the Deal's header and the Audit Summary ─────────────────
def test_the_deal_counts_the_loan_as_the_audit_summary_does(journey) -> None:
    from audit_core.uc03_p2_audit_summary import audit_summary

    _statement(journey, UTKAL)  # Finance D/O 10,60,000; no disbursement on a finance record
    view = _deal(journey)
    paid = view["summary"]["paid"]
    assert D(paid["loan"]) == D(1060000) and paid["loanSource"] == "NDC (account statement)"
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        summary = audit_summary(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert D(summary["funding"]["loanDisbursed"]) == D(paid["loan"])
    assert D(summary["funding"]["received"]) == D(paid["total"])
    if view["summary"]["balanceDue"] is not None:
        assert D(summary["settlement"]["balance"]) == D(view["summary"]["balanceDue"])
