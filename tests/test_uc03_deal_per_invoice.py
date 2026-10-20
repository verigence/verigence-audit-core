"""Billed / deal maths per invoice (2026-10-20 product owner rules R1, R2, R6).

R1  Ex-showroom = the vehicle invoice's own printed selling price x (1 + its
    own GST rate); each printed (pre-GST) discount shown GST-inclusive with
    the printed figure kept; ex-showroom less discounts = the invoice total.
R2  Each invoice serves only its own lines, counted once (DMS + Tally copy of
    one invoice number = one invoice); the wholesale invoice is never a
    customer bill; voided documents stop counting.
R6  The invoice is the source of truth; the booking form never overwrites it.

The example invoice (the one the product owner checked by hand): selling
price 7,22,033.90; discounts dealer 8,474.58, OEM 11,017.00 and corporate
1,525.00 (21,016.58 printed before GST); taxable 7,01,017.32; CGST 9% and
SGST 9% 63,091.56 each; grand total 8,27,200.44.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_evidence,
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_invoice_materialization as im
from audit_core.db import set_tenant_context
from audit_core.uc03_delivery_post_extraction_materialization import (
    _documents_from_durable_store,
)
from audit_core.uc03_p2_journey360 import deal

EXAMPLE = {
    "invoice_purpose": "VEHICLE_SALE",
    "invoice_nature": "TAX_INVOICE",
    "invoice_number": "DMS/26/0815",
    "invoice_date": "2026-10-05",
    "vin_number": "MA1NE2XO3T4000001",
    "gross_amount_before_discount": "722033.90",
    "invoice_discount_amount": "21016.58",
    "taxable_amount": "701017.32",
    "cgst_rate": "9",
    "cgst_amount": "63091.56",
    "sgst_rate": "9",
    "sgst_amount": "63091.56",
    "grand_total_amount": "827200.44",
    "line_items": [
        {"line_category": "VEHICLE", "description_raw": "XUV 3XO AX5 PETROL MT", "gross_amount": 722033.90,
         "discount_amount": 21016.58, "taxable_amount": 701017.32, "tax_rate": 18, "net_amount": 827200.44},
        {"line_category": "DISCOUNT_LINE", "description_raw": "Dealer Discount", "taxable_amount": -8474.58},
        {"line_category": "DISCOUNT_LINE", "description_raw": "OEM Scheme Discount", "taxable_amount": -11017.00},
        {"line_category": "DISCOUNT_LINE", "description_raw": "Corporate Discount", "taxable_amount": -1525.00},
    ],
}


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="perinv")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _materialize(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        documents = _documents_from_durable_store(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, stage_code="BOOKING")
        return im.materialize_reviewed_invoices(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, documents=documents,
            actor_id="tester", stage_code="BOOKING",
        )


def _deal(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return deal(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _sql(journey, sql, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _rows(view):
    return {r["key"]: r for g in view["categories"] for r in g["components"]}


def _discounts(view):
    return {d["key"]: d for d in view["discounts"]}


def _open_findings(journey, like):
    return [r[0] for r in _sql(
        journey, "SELECT rule_key FROM auditcore.audit_findings WHERE tenant_id=:t AND journey_id=:j "
                 "AND finding_status IN ('OPEN','ACKNOWLEDGED') AND rule_key LIKE :like ORDER BY rule_key",
        like=like,
    ).all()]


def _line(journey, key):
    return _sql(journey, "SELECT actual_amount, source_reference FROM auditcore.commercial_lines "
                         "WHERE tenant_id=:t AND journey_id=:j AND component_key=:k", k=key).mappings().one_or_none()


# ── reading one invoice ──────────────────────────────────────────────────────
def test_the_example_invoice_reads_as_the_product_owner_worked_it():
    reading = im.read_invoice("customer_invoice_dms", EXAMPLE)
    assert reading["issues"] == []
    assert reading["lines"] == {"ex_showroom_price": Decimal("852000.00")}
    discounts = {k: (v["amount"], v["printed"]) for k, v in reading["discounts"].items()}
    assert discounts == {
        "ADDITIONAL_DISCOUNT": (Decimal("10000.00"), Decimal("8474.58")),
        "CASH_DISCOUNT": (Decimal("13000.06"), Decimal("11017.00")),
        "CORPORATE_PRIVILEGE": (Decimal("1799.50"), Decimal("1525.00")),
    }
    total = sum((v["amount"] for v in reading["discounts"].values()), Decimal(0))
    assert total == Decimal("24799.56")
    # ex-showroom less every discount is the invoice's own total.
    assert reading["lines"]["ex_showroom_price"] - total == Decimal("827200.44") == reading["netPayable"]


def test_header_discount_and_discount_lines_are_one_discount_never_two():
    reading = im.read_invoice("customer_invoice_dms", EXAMPLE)
    assert sum((v["printed"] for v in reading["discounts"].values()), Decimal(0)) == Decimal("21016.58")
    disagree = im.read_invoice("customer_invoice_dms", {**EXAMPLE, "invoice_discount_amount": "25000"})
    # The lines (classified) are used; the header disagreeing is a task.
    assert [i["code"] for i in disagree["issues"]] == ["DISCOUNT_HEADER_LINES_DISAGREE"]


def test_a_header_only_discount_stays_its_own_key_and_the_selling_price_is_rebuilt_from_this_invoice():
    raw = {k: v for k, v in EXAMPLE.items() if k not in ("line_items", "gross_amount_before_discount")}
    reading = im.read_invoice("tax_invoice_tally", raw)
    assert reading["vehicle"]["sellingPriceSource"] == "TAXABLE_PLUS_DISCOUNT"
    assert reading["lines"]["ex_showroom_price"] == Decimal("852000.00")
    assert list(reading["discounts"]) == ["UNCLASSIFIED_DISCOUNT"]
    assert reading["discounts"]["UNCLASSIFIED_DISCOUNT"]["amount"] == Decimal("24799.56")


def test_igst_and_every_vehicle_sale_type_by_purpose():
    raw = {k: v for k, v in EXAMPLE.items() if not k.startswith(("cgst", "sgst"))}
    raw.update({"igst_rate": "18", "igst_amount": "126183.12"})
    for document_type in ("customer_invoice_dms", "tax_invoice_tally", "tax_invoice_dms", "tax_invoice",
                          "customer_invoice_dms_v2", "invoice_generic"):
        reading = im.read_invoice(document_type, raw)
        assert reading["family"] == "VEHICLE", document_type
        assert reading["lines"]["ex_showroom_price"] == Decimal("852000.00"), document_type
    generic_accessory = im.read_invoice("invoice_generic", {"invoice_purpose": "ACCESSORY",
                                                            "grand_total_amount": "4746.01"})
    assert generic_accessory["family"] == "ACCESSORY"
    assert generic_accessory["lines"] == {"accessories_cost": Decimal("4746.01")}


def test_the_wholesale_invoice_is_the_dealers_purchase_never_a_customer_line():
    reading = im.read_invoice("wholesale_invoice", {**EXAMPLE, "invoice_number": "WS-1"})
    assert reading["family"] == "WHOLESALE" and reading["lines"] == {} and reading["discounts"] == {}
    assert reading["dealerPurchase"]["exShowroom"] == Decimal("852000.00")
    assert im.read_invoice("customer_invoice_dms", {"invoice_purpose": "VEHICLE_WHOLESALE"})["family"] == "WHOLESALE"


def test_an_accessory_invoice_bills_its_grand_total_and_checks_its_lines():
    reading = im.read_invoice("accessory_invoice_dms", {
        "grand_total_amount": "7457.01",
        "line_items": [{"line_category": "ACCESSORY_GENUINE", "quantity": 4, "unit_rate": "500"},
                       {"line_category": "OTHER", "net_amount": "2711"}],
    })
    assert reading["lines"] == {"accessories_cost": Decimal("7457.01")}
    # 4 x 500 + 2,711 = 4,711: the lines do not add up to the total.
    assert [i["code"] for i in reading["issues"]] == ["LINES_DO_NOT_ADD_UP"]


def test_a_credit_note_is_read_by_its_narration():
    reading = im.read_invoice("credit_note", {"grand_total_amount": "50000", "taxable_amount": "42372.88",
                                              "narration": "Exchange bonus passed on"})
    assert reading["discounts"]["EXCHANGE_BONUS"]["amount"] == Decimal(50000)
    assert reading["netPayable"] == Decimal(-50000)
    plain = im.read_invoice("credit_note", {"grand_total_amount": "5000"})
    assert list(plain["discounts"]) == ["ADDITIONAL_DISCOUNT"]


def test_the_invoices_own_arithmetic_is_checked_to_the_rupee():
    wrong_total = im.read_invoice("customer_invoice_dms", {**EXAMPLE, "grand_total_amount": "830000"})
    assert [i["code"] for i in wrong_total["issues"]] == ["TOTAL_DOES_NOT_ADD_UP"]
    wrong_gst = im.read_invoice("customer_invoice_dms", {**EXAMPLE, "cgst_amount": "70000", "grand_total_amount": "834108.88"})
    assert "GST_NOT_AT_RATE" in [i["code"] for i in wrong_gst["issues"]]


# ── the example end to end: materialise, then the Deal ──────────────────────
def test_the_example_invoice_end_to_end(journey):
    document_id = add_ready_document(journey, "customer_invoice_dms", **EXAMPLE)
    _materialize(journey)

    line = _line(journey, "ex_showroom_price")
    assert line["actual_amount"] == Decimal("852000.00")
    assert line["source_reference"] == f"customer_invoice_dms:{document_id}"
    stored = {r["discount_key"]: (r["actual_discount_amount"], r["details"]["printedBeforeGst"])
              for r in _sql(journey, "SELECT discount_key, actual_discount_amount, details FROM "
                                     "auditcore.discount_applications WHERE tenant_id=:t AND journey_id=:j").mappings()}
    assert stored == {"ADDITIONAL_DISCOUNT": (Decimal("10000.00"), "8474.58"),
                      "CASH_DISCOUNT": (Decimal("13000.06"), "11017.00"),
                      "CORPORATE_PRIVILEGE": (Decimal("1799.50"), "1525.00")}
    assert _open_findings(journey, "INVOICE_%") == []

    view = _deal(journey)
    vehicle = _rows(view)["ex_showroom_price"]
    assert vehicle["billed"] == "852000.00" and vehicle["billedFrom"] == "invoice"
    discounts = _discounts(view)
    assert discounts["ADDITIONAL_DISCOUNT"]["billed"] == "10000.00"
    assert discounts["ADDITIONAL_DISCOUNT"]["billedPrinted"] == "8474.58"
    assert discounts["CASH_DISCOUNT"]["billed"] == "13000.06"
    assert discounts["CORPORATE_PRIVILEGE"]["billed"] == "1799.50"
    summary = view["summary"]
    assert Decimal(summary["discounts"]["billed"]) == Decimal("24799.56")
    assert Decimal(summary["invoiceNetPayable"]) == Decimal("827200.44")
    assert Decimal(summary["net"]["current"]) == Decimal("827200.44")
    [invoice] = view["invoices"]
    assert invoice["counted"] is True and invoice["exShowroom"] == "852000.00" and invoice["issues"] == []


def test_two_vehicle_invoices_are_two_invoices_one_prices_the_deal_and_a_team_lead_is_asked(journey):
    first = add_ready_document(journey, "customer_invoice_dms", **{**EXAMPLE, "invoice_number": "DMS/26/0815",
                                                                  "invoice_date": "2026-10-05"})
    second = add_ready_document(journey, "customer_invoice_dms", **{
        **EXAMPLE, "invoice_number": "DMS/26/0901", "invoice_date": "2026-10-09",
        "line_items": [], "invoice_discount_amount": "30000", "gross_amount_before_discount": "731017.32",
    })
    _materialize(journey)
    # One source row per document, never one overwriting the other.
    rows = _sql(journey, "SELECT source_document_id FROM auditcore.commercial_line_source_values WHERE "
                         "tenant_id=:t AND journey_id=:j AND component_key='ex_showroom_price'").scalars().all()
    assert sorted(map(str, rows)) == sorted([str(first), str(second)])
    assert _open_findings(journey, "MULTIPLE_VEHICLE_INVOICES") == ["MULTIPLE_VEHICLE_INVOICES"]

    view = _deal(journey)
    # The later invoice prices the deal (deterministic, never upload order):
    # 7,31,017.32 x 1.18 = 8,62,600.44; its 30,000 discount x 1.18 = 35,400.
    assert _rows(view)["ex_showroom_price"]["billed"] == "862600.44"
    assert _discounts(view)["UNCLASSIFIED_DISCOUNT"]["billed"] == "35400.00"
    assert "CASH_DISCOUNT" not in _discounts(view) or _discounts(view)["CASH_DISCOUNT"]["billed"] is None
    assert Decimal(view["summary"]["net"]["current"]) == Decimal("827200.44")
    assert _line(journey, "ex_showroom_price")["source_reference"] == f"customer_invoice_dms:{second}"
    counted = [i["documentId"] for i in view["invoices"] if i["counted"]]
    assert counted == [str(second)]


def test_dms_and_tally_copies_of_one_invoice_count_once_and_a_disagreement_is_flagged(journey):
    add_ready_document(journey, "customer_invoice_dms", **EXAMPLE)
    tally = {k: v for k, v in EXAMPLE.items() if k != "line_items"}
    tally.update({"invoice_number": "dms/26/ 0815", "invoice_discount_amount": "21516.58",
                  "taxable_amount": "700517.32"})
    add_ready_document(journey, "tax_invoice_tally", **tally)
    _materialize(journey)
    assert len(_open_findings(journey, "INVOICE_COPIES_DISAGREE:%")) == 1

    view = _deal(journey)
    assert Decimal(view["summary"]["invoiceNetPayable"]) == Decimal("827200.44")  # once, not twice
    assert Decimal(view["summary"]["net"]["current"]) == Decimal("827200.44")
    counted = [i for i in view["invoices"] if i["counted"]]
    assert len(counted) == 1 and len(counted[0]["sameInvoiceAs"]) == 1
    # The copies print the same ex-showroom; they disagree on the discount.
    assert _rows(view)["ex_showroom_price"]["billed"] == "852000.00"
    assert any("INVOICES_DISAGREE" in d["flags"] for d in view["discounts"])


def test_the_wholesale_invoice_never_bills_the_customer(journey):
    add_ready_document(journey, "booking_form", ex_showroom_price="852000")
    add_ready_document(journey, "wholesale_invoice", **{**EXAMPLE, "invoice_number": "WS-1",
                                                        "invoice_purpose": "VEHICLE_WHOLESALE"})
    _materialize(journey)
    assert _line(journey, "ex_showroom_price") is None
    assert _sql(journey, "SELECT count(*) FROM auditcore.commercial_line_source_values WHERE tenant_id=:t "
                         "AND journey_id=:j AND source_document_type='wholesale_invoice'").scalar_one() == 0
    view = _deal(journey)
    vehicle = _rows(view)["ex_showroom_price"]
    assert vehicle["billed"] is None and Decimal(vehicle["booking"]) == 852000
    assert view["summary"]["invoiceIsSourceOfTruth"] is False
    [purchase] = view["dealerPurchase"]
    assert purchase["number"] == "WS-1" and purchase["exShowroom"] == "852000.00"


def test_a_voided_invoice_stops_counting_and_the_actuals_fall_back(journey):
    from audit_core.uc03_deal_source_history import upsert_commercial_actual

    booking = add_ready_document(journey, "booking_form", ex_showroom_price="850000")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        upsert_commercial_actual(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                 component_key="ex_showroom_price", amount=Decimal(850000),
                                 document_type="booking_form", document_id=booking, evidence_id=None)
    invoice = add_ready_document(journey, "customer_invoice_dms", **EXAMPLE)
    _materialize(journey)
    assert _line(journey, "ex_showroom_price")["actual_amount"] == Decimal("852000.00")

    _sql(journey, "UPDATE auditcore.evidence SET association_status='VOIDED' WHERE tenant_id=:t AND "
                  "di_document_id=:d", d=invoice)
    _materialize(journey)  # the booking form is still on file: the pass re-derives everything

    line = _line(journey, "ex_showroom_price")
    assert line["actual_amount"] == Decimal(850000) and line["source_reference"] == f"booking_form:{booking}"
    withdrawn = _sql(journey, "SELECT actual_discount_amount, details->>'origin' FROM auditcore.discount_applications "
                              "WHERE tenant_id=:t AND journey_id=:j").all()
    assert withdrawn and all(amount is None and origin == "INVOICE_WITHDRAWN" for amount, origin in withdrawn)
    view = _deal(journey)
    assert view["summary"]["invoiceIsSourceOfTruth"] is False
    assert _rows(view)["ex_showroom_price"]["billed"] is None
    assert all(d["billed"] is None for d in view["discounts"])
    assert view["invoices"] == []


def test_an_accessory_invoice_alone_zeroes_neither_the_vehicle_nor_the_insurance(journey):
    _sql(journey, "INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount) "
                  "VALUES (:t, :j, 'ex_showroom_price', 852000), (:t, :j, 'insurance_amount', 45225)")
    add_ready_document(journey, "booking_form", ex_showroom_price="852000", insurance_amount="45225")
    add_ready_document(journey, "accessory_invoice_dms", invoice_number="ACC-7", grand_total_amount="7457.01")
    view = _deal(journey)
    rows = _rows(view)
    assert rows["ex_showroom_price"]["awaiting"] == "vehicle invoice"
    assert rows["insurance_amount"]["awaiting"] == "insurance cover"
    assert Decimal(rows["ex_showroom_price"]["current"]) == 852000
    assert Decimal(rows["insurance_amount"]["current"]) == 45225
    assert Decimal(rows["accessories_cost"]["current"]) == Decimal("7457.01")
    summary = view["summary"]
    assert Decimal(summary["net"]["current"]) == Decimal("904682.01")
    assert Decimal(summary["payable"]) == Decimal("904682.01")
    assert not any("NOT_INVOICED" in r["flags"] for r in rows.values())


def test_utkal_like_totals_variance_is_current_net_less_standard_net(journey):
    """The Utkal deal shape: list ex-showroom 10,29,900.54, TCS on the list,
    RTO 1,03,730, insurance 45,225, RSA 2,021, FASTag 500; a consumer
    entitlement of 30,000 and unclaimed exchange / scrappage entitlements.
    The vehicle invoice bills the list price, the 30,000 consumer offer and
    a 5,000 dealer discount (both printed before GST)."""
    standards = {"ex_showroom_price": "1029900.54", "tcs_amount": "10299.01", "registration_charges": "103730",
                 "insurance_amount": "45225", "rsa_amount": "2021", "fastag_amount": "500"}
    for key, amount in standards.items():
        _sql(journey, "INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount) "
                      "VALUES (:t, :j, :k, :a)", k=key, a=amount)
    for key, amount, result in (("CASH_DISCOUNT", "30000", "ELIGIBLE"), ("EXCHANGE_BONUS", "50000", "ELIGIBLE_UNCLAIMED"),
                                ("SCRAPPAGE_BONUS_COD", "30000", "ELIGIBLE_UNCLAIMED")):
        _sql(journey, "INSERT INTO auditcore.discount_applications (tenant_id, journey_id, discount_key, "
                      "standard_eligible_amount, eligibility_result, actual_source_kind) "
                      "VALUES (:t, :j, :k, :a, :r, 'CALCULATED')", k=key, a=amount, r=result)
    add_ready_document(journey, "booking_form", ex_showroom_price="1029900.54", rsa_amount="2021",
                       fastag_amount="500", insurance_amount="45225", road_tax_registration="103730")
    add_ready_document(journey, "customer_invoice_dms", invoice_purpose="VEHICLE_SALE", invoice_number="UTK/1", invoice_date="2026-09-20", gross_amount_before_discount="872797.07", taxable_amount="843136.05", cgst_amount="75882.24", sgst_amount="75882.24", grand_total_amount="994900.53", line_items=[
            {"line_category": "DISCOUNT_LINE", "description_raw": "M&M Consumer Offer", "taxable_amount": 25423.73},
            {"line_category": "DISCOUNT_LINE", "description_raw": "Dealer Discount", "taxable_amount": 4237.29},
        ])
    add_ready_document(journey, "insurance_cover", premium_amount="45225")
    challan = uuid4()
    add_evidence(journey, di_document_id=challan, document_type_key="rto_challan")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        for key, amount in (("road_tax_amount", "100000"), ("registration_charges", "3730")):
            im._upsert_commercial_line(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                       component_key=key, amount=Decimal(amount), document_type="rto_challan",
                                       document_id=challan, evidence_id=None)
    view = _deal(journey)
    rows, discounts, summary = _rows(view), _discounts(view), view["summary"]

    assert rows["ex_showroom_price"]["billed"] == "1029900.54" and Decimal(rows["ex_showroom_price"]["variance"]) == 0
    # TCS is due on what the car sold for (under ₹10 lakh here), not on the list price.
    assert Decimal(rows["tcs_amount"]["standard"]) == 0
    assert Decimal(rows["road_tax_registration"]["billed"]) == 103730
    assert rows["rsa_amount"]["awaiting"] == "RSA invoice" and rows["fastag_amount"]["awaiting"] == "FASTag invoice"
    assert discounts["CASH_DISCOUNT"]["billed"] == "30000.00" and Decimal(discounts["CASH_DISCOUNT"]["variance"]) == 0
    assert discounts["ADDITIONAL_DISCOUNT"]["billed"] == "5000.00"
    # Unclaimed exchange / scrappage are shown, not subtracted from the standard.
    assert discounts["EXCHANGE_BONUS"]["inStandard"] is False and discounts["SCRAPPAGE_BONUS_COD"]["inStandard"] is False
    assert Decimal(summary["net"]["standard"]) == Decimal("1151376.54")
    assert Decimal(summary["net"]["current"]) == Decimal("1146376.53")
    assert Decimal(summary["variance"]["currentVsStandard"]) == (
        Decimal(summary["net"]["current"]) - Decimal(summary["net"]["standard"])) == Decimal("-5000.01")


def test_the_booking_form_never_overwrites_an_invoice_actual(journey):
    from audit_core.uc03_v2_review_materialization import _materialize_commercial_lines

    add_ready_document(journey, "customer_invoice_dms", **EXAMPLE)
    _materialize(journey)
    booking = uuid4()
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        _materialize_commercial_lines(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                      document_id=booking, evidence_id=None,
                                      values={"ex_showroom_price": Decimal(860000)})
    assert _line(journey, "ex_showroom_price")["actual_amount"] == Decimal("852000.00")
    assert _sql(journey, "SELECT amount FROM auditcore.commercial_line_source_values WHERE tenant_id=:t AND "
                         "journey_id=:j AND source_document_type='booking_form'").scalar_one() == Decimal(860000)


def test_a_loan_already_received_as_a_receipt_is_not_counted_again(journey):
    add_receipt_payment(journey, amount="400000", receipt_number="R-LOAN", receipt_date="2026-09-20")
    _sql(journey, "INSERT INTO auditcore.finance_records (tenant_id, journey_id, provider_name, "
                  "loan_disbursement_amount) VALUES (:t, :j, 'HDFC Bank', 400000)")
    paid = _deal(journey)["summary"]["paid"]
    assert Decimal(paid["receipts"]) == 400000 and Decimal(paid["loan"]) == 0 and Decimal(paid["total"]) == 400000


def test_a_closed_deal_without_the_vehicle_invoice_line_is_not_invoiced(journey):
    _sql(journey, "INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount) "
                  "VALUES (:t, :j, 'fastag_amount', 500)")
    add_ready_document(journey, "booking_form", fastag_amount="500")
    add_ready_document(journey, "customer_invoice_dms", **EXAMPLE)
    assert _rows(_deal(journey))["fastag_amount"]["awaiting"] == "FASTag invoice"
    add_ready_document(journey, "gate_pass", delivery_date=(datetime.now(UTC).date() - timedelta(days=30)).isoformat())
    fastag = _rows(_deal(journey))["fastag_amount"]
    assert fastag["awaiting"] is None and fastag["current"] == "0" and "NOT_INVOICED" in fastag["flags"]


def test_the_header_names_the_plan_used_and_no_price_master_shows_why(journey, monkeypatch):
    """MERGE adapter for uc03_sku_candidates.journey_price_plan (PR #468): the
    header names the version the standards came from; with none in force on
    the pricing date the Standard column carries the reason, not figures."""
    from audit_core import uc03_p2_journey360 as model

    _sql(journey, "INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount) "
                  "VALUES (:t, :j, 'ex_showroom_price', 852000)")
    add_ready_document(journey, "customer_invoice_dms", **EXAMPLE)
    monkeypatch.setattr(model, "_journey_price_plan", lambda *a, **k: {
        "onDate": "2026-10-05", "reason": None,
        "plan": {"price_list_name": "MAHINDRA_PV", "version_no": 5, "effective_from": "2026-10-03"}})
    view = _deal(journey)
    assert view["sku"]["priceList"] == "MAHINDRA_PV" and view["sku"]["priceListVersion"] == 5
    assert view["standardUnavailable"] is None and Decimal(_rows(view)["ex_showroom_price"]["standard"]) == 852000

    monkeypatch.setattr(model, "_journey_price_plan", lambda *a, **k: {
        "onDate": "2026-10-05", "plan": None, "reason": "No price master in force on 2026-10-05"})
    view = _deal(journey)
    assert view["standardUnavailable"] == "No price master in force on 2026-10-05"
    assert _rows(view)["ex_showroom_price"]["standard"] is None
    assert view["summary"]["net"]["standard"] is None and view["summary"]["variance"]["currentVsStandard"] is None
