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

from datetime import UTC, date, datetime, timedelta
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
    # The document type decides (review S9); a purpose naming another kind of
    # invoice is a PC check, never a silent re-classification.
    misread = im.read_invoice("customer_invoice_dms", {**EXAMPLE, "invoice_purpose": "VEHICLE_WHOLESALE"})
    assert misread["family"] == "VEHICLE" and "PURPOSE_CONFLICTS_WITH_TYPE" in [i["code"] for i in misread["issues"]]
    accessory = im.read_invoice("accessory_invoice_dms", {**EXAMPLE, "grand_total_amount": "7457.01"})
    assert accessory["family"] == "ACCESSORY" and accessory["lines"] == {"accessories_cost": Decimal("7457.01")}
    assert "PURPOSE_CONFLICTS_WITH_TYPE" in [i["code"] for i in accessory["issues"]]


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
    # A different vehicle (VIN): two sale invoices, not two copies of one.
    second = add_ready_document(journey, "customer_invoice_dms", **{
        **EXAMPLE, "invoice_number": "DMS/26/0901", "invoice_date": "2026-10-09", "vin_number": "MA1NE2XO3T4000002",
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

    # No booking date yet: no pricing date to judge by; the header says why,
    # the standards on record stay (review M2).
    monkeypatch.setattr(model, "_journey_price_plan", lambda *a, **k: {
        "onDate": None, "plan": None, "reason": "No booking date to price the deal on"})
    view = _deal(journey)
    assert view["standardUnavailable"] is None and view["sku"]["priceListReason"].startswith("No booking date")
    assert Decimal(_rows(view)["ex_showroom_price"]["standard"]) == 852000


# ── review of PR #470 (2026-10-21) ───────────────────────────────────────────
def _tally_copy(**overrides):
    tally = {k: v for k, v in EXAMPLE.items() if k != "line_items"}
    tally.update({"invoice_number": "TL/2231", **overrides})
    return tally


def test_dms_and_tally_copies_of_one_sale_with_different_numbers_are_one_invoice():
    """Review B1: one vehicle (VIN) is one sale invoice, whatever number each
    copy prints; a disagreement between them is flagged, never added."""
    dms = im.read_invoice("customer_invoice_dms", EXAMPLE, document_id="dms")
    tally = im.read_invoice("tax_invoice_tally", _tally_copy(), document_id="tally")
    selection = im.select_invoices([dms, tally])
    assert len(selection["counted"]) == 1 and selection["multipleVehicle"] is False
    assert selection["netPayable"] == Decimal("827200.44")
    assert selection["lines"]["ex_showroom_price"] == Decimal("852000.00")
    # Different discount reading on the Tally copy: one invoice, flagged.
    assert selection["counted"][0]["disagreements"]
    other_vehicle = im.read_invoice("tax_invoice_tally", _tally_copy(vin_number="MA1OTHERVIN000009"),
                                    document_id="other")
    assert im.select_invoices([dms, other_vehicle])["multipleVehicle"] is True


def test_dms_and_tally_accessory_bills_pair_by_total_and_date_else_the_pc_is_asked():
    dms = im.read_invoice("accessory_invoice_dms", {"invoice_number": "ACC/DMS/0091", "invoice_date": "2026-10-05",
                                                    "grand_total_amount": "7457.01"}, document_id="a")
    same = im.read_invoice("accessory_invoice_tally", {"invoice_number": "TL-2231", "invoice_date": "2026-10-06",
                                                       "grand_total_amount": "7457.01"}, document_id="b")
    one = im.select_invoices([dms, same])
    assert len(one["counted"]) == 1 and one["lines"]["accessories_cost"] == Decimal("7457.01")
    assert one["sameBillQuestions"] == []
    other = im.read_invoice("accessory_invoice_tally", {"invoice_number": "TL-2231", "invoice_date": "2026-10-06",
                                                        "grand_total_amount": "4746.01"}, document_id="c")
    two = im.select_invoices([dms, other])
    assert len(two["counted"]) == 2 and two["lines"]["accessories_cost"] == Decimal("12203.02")
    assert len(two["sameBillQuestions"]) == 1


def test_a_vehicle_line_rate_counts_only_when_it_agrees_with_the_header():
    accessory = {"line_category": "ACCESSORY_GENUINE", "description_raw": "Mats", "net_amount": "5900"}
    vehicle_line = {"line_category": "VEHICLE", "gross_amount": "722033.90", "taxable_amount": "701017.32"}
    # 9% on the line = each of CGST and SGST: agrees with the header's 18%.
    per_component = im.read_invoice("customer_invoice_dms", {**EXAMPLE, "line_items": [
        {**vehicle_line, "tax_rate": 9}, accessory,
        *[li for li in EXAMPLE["line_items"] if li["line_category"] == "DISCOUNT_LINE"]]})
    assert per_component["lines"]["ex_showroom_price"] == Decimal("852000.00")
    assert per_component["lines"]["accessories_cost"] == Decimal(5900)
    # 28% on the line against 18% on the header: unclear, no figure.
    disagree = im.read_invoice("customer_invoice_dms", {**EXAMPLE, "line_items": [{**vehicle_line, "tax_rate": 28}]})
    assert "ex_showroom_price" not in disagree["lines"]
    assert [i["code"] for i in disagree["issues"]] == ["RATE_UNCLEAR"]


def test_a_vehicle_price_mixed_with_add_ons_is_not_read_from_the_header():
    reading = im.read_invoice("customer_invoice_dms", {**EXAMPLE, "line_items": [
        {"line_category": "VEHICLE", "description_raw": "XUV 3XO"},
        {"line_category": "FASTAG", "net_amount": "500"}]})
    assert "ex_showroom_price" not in reading["lines"] and reading["lines"]["fastag_amount"] == Decimal(500)
    assert "VEHICLE_LINE_NOT_READ" in [i["code"] for i in reading["issues"]]


def test_a_rate_printed_on_one_component_is_never_doubled_blindly():
    base = {k: v for k, v in EXAMPLE.items() if k not in ("sgst_rate", "line_items")}
    total_in_cgst = im.read_invoice("customer_invoice_dms", {**base, "cgst_rate": "18"})
    assert total_in_cgst["vehicle"]["gstRate"] == Decimal("0.18")
    assert total_in_cgst["lines"]["ex_showroom_price"] == Decimal("852000.00")
    per_component = im.read_invoice("customer_invoice_dms", {**base, "cgst_rate": "9"})
    assert per_component["vehicle"]["gstRate"] == Decimal("0.18")
    unconfirmed = im.read_invoice("customer_invoice_dms", {
        k: v for k, v in base.items() if k not in ("cgst_amount", "sgst_amount")})
    assert "ex_showroom_price" not in unconfirmed["lines"]
    assert "RATE_UNCLEAR" in [i["code"] for i in unconfirmed["issues"]]


def test_a_credit_note_reversing_a_vehicle_invoice_cancels_it():
    """Review S4: invoice 0815, a credit note for its whole total naming it,
    then a reissue: the reissue alone is billed, no discount appears."""
    original = im.read_invoice("customer_invoice_dms", {**EXAMPLE, "invoice_date": "2026-10-10"}, document_id="o")
    note = im.read_invoice("credit_note", {"invoice_number": "CN-5", "grand_total_amount": "827200.44",
                                          "narration": "Cancellation of invoice DMS/26/0815"}, document_id="cn")
    reissue = im.read_invoice("customer_invoice_dms", {**EXAMPLE, "invoice_number": "DMS/26/0901",
                                                       "invoice_date": "2026-10-15"}, document_id="r")
    selection = im.select_invoices([original, note, reissue])
    assert selection["multipleVehicle"] is False
    assert selection["vehicle"]["representative"]["documentId"] == "r"
    assert selection["netPayable"] == Decimal("827200.44")
    # Only the reissue's own dealer discount; the credit note adds none.
    assert selection["discounts"]["ADDITIONAL_DISCOUNT"]["amount"] == Decimal("10000.00")
    assert {r["documentId"] for g in selection["cancelled"] for r in g["copies"]} == {"o", "cn"}
    partial = im.read_invoice("credit_note", {"invoice_number": "CN-6", "grand_total_amount": "5000",
                                             "narration": "Against invoice DMS/26/0901"}, document_id="p")
    kept = im.select_invoices([reissue, partial])
    assert kept["discounts"]["ADDITIONAL_DISCOUNT"]["amount"] == Decimal("15000.00")
    assert kept["netPayable"] == Decimal("822200.44")


def test_a_generic_copy_without_a_purpose_joins_the_vehicle_invoice():
    dms = im.read_invoice("customer_invoice_dms", EXAMPLE, document_id="dms")
    generic = im.read_invoice("invoice_generic", {k: v for k, v in EXAMPLE.items() if k != "invoice_purpose"},
                              document_id="generic")
    assert generic["family"] == "OTHER"
    selection = im.select_invoices([dms, generic])
    assert len(selection["counted"]) == 1 and selection["netPayable"] == Decimal("827200.44")


def test_the_shared_r1_reading_for_every_caller():
    reading = im.vehicle_invoice_ex_showroom(EXAMPLE, "customer_invoice_dms")
    assert reading["exShowroom"] == Decimal("852000.00") and reading["rate"] == Decimal("0.18")
    assert reading["saleValue"] == Decimal("827200.44") and reading["issues"] == []
    assert reading["discounts"]["CASH_DISCOUNT"] == {"amount": Decimal("13000.06"), "printed": Decimal("11017.00")}


def test_a_stale_spelling_of_a_type_never_wins(journey):
    """Review S6: two spellings of one type on one document (rows written
    before 0153): the newest row stands."""
    from audit_core.uc03_deal_source_history import active_source_rows

    booking = add_ready_document(journey, "booking_docket", insurance_amount="47000")
    for spelling, amount, age in (("booking docket", "45000", "2 days"), ("booking_docket", "47000", "1 hour")):
        _sql(journey, "INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, "
                      "component_key, source_document_type, amount, source_document_id, updated_at_utc) "
                      "VALUES (:t, :j, 'COMMERCIAL', 'insurance_amount', :d, :a, :doc, now() - CAST(:age AS interval))",
             d=spelling, a=amount, doc=booking, age=age)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        rows = active_source_rows(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert [(r["source_document_type"], r["amount"]) for r in rows] == [("booking_docket", Decimal(47000))]
    # RN1: written in one pass (same timestamp), the normalised spelling wins.
    for spelling, amount in (("Booking Docket", "90000"), ("booking_docket", "91000")):
        _sql(journey, "INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, "
                      "component_key, source_document_type, amount, source_document_id, updated_at_utc) "
                      "VALUES (:t, :j, 'COMMERCIAL', 'road_tax_amount', :d, :a, :doc, '2026-10-20T10:00:00Z')",
             d=spelling, a=amount, doc=booking)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        rows = active_source_rows(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert [r["amount"] for r in rows if r["component_key"] == "road_tax_amount"] == [Decimal(91000)]
    assert Decimal(_rows(_deal(journey))["insurance_amount"]["booking"]) == 47000


def test_a_line_awaiting_its_document_with_no_booking_figure_is_listed_not_priced(journey):
    """Review S7: no booking figure -> awaiting with no amount, out of both
    nets and Balance due, listed under "awaiting"; never the standard."""
    _sql(journey, "INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount) "
                  "VALUES (:t, :j, 'ex_showroom_price', 852000), (:t, :j, 'registration_charges', 90000), "
                  "(:t, :j, 'rsa_amount', 2021)")
    add_ready_document(journey, "customer_invoice_dms", **EXAMPLE)
    view = _deal(journey)
    rows, summary = _rows(view), view["summary"]
    assert rows["road_tax_registration"]["awaiting"] == "RTO challan" and rows["road_tax_registration"]["current"] is None
    assert rows["rsa_amount"]["awaiting"] == "RSA invoice" and rows["rsa_amount"]["current"] is None
    assert {a["key"] for a in summary["awaiting"]} == {"road_tax_registration", "rsa_amount"}
    assert summary["awaitingDocuments"] == 2
    assert Decimal(summary["net"]["current"]) == Decimal("827200.44")
    assert Decimal(summary["net"]["standard"]) == 852000
    assert Decimal(summary["balanceDue"]) == Decimal("827200.44")


# ── re-review of PR #470 and the live Mahindra Orissa data (2026-10-21) ──────
VIN = "MA1RM2TVXT5H14641"


def _vehicle(document_id, document_type="customer_invoice_dms", **overrides):
    raw = {**EXAMPLE, "vin_number": VIN, **overrides}
    raw = {k: v for k, v in raw.items() if v is not None}
    return im.read_invoice(document_type, raw, document_id=document_id)


def _tally(document_id, **overrides):
    raw = {k: v for k, v in EXAMPLE.items() if k != "line_items"}
    raw.update({"vin_number": None, **overrides})
    return im.read_invoice("tax_invoice_tally", {k: v for k, v in raw.items() if v is not None},
                           document_id=document_id)


def _note(document_id, amount="827200.44", **fields):
    return im.read_invoice("credit_note", {"invoice_number": document_id.upper(), "grand_total_amount": amount,
                                           **fields}, document_id=document_id)


def test_a_same_day_reissue_survives_the_credit_note_that_cancels_the_original():
    """RB1: only the cancelled bill (and its copy from the other system) goes."""
    original = _vehicle("o", invoice_number="DMS/26/0815", invoice_date="2026-10-10")
    note = _note("cn", narration="Cancellation of invoice DMS/26/0815 - name correction")
    reissue = _vehicle("r", invoice_number="DMS/26/0816", invoice_date="2026-10-10")
    selection = im.select_invoices([original, note, reissue])
    assert selection["vehicle"]["representative"]["documentId"] == "r"
    assert selection["netPayable"] == Decimal("827200.44") and selection["multipleVehicle"] is False
    assert {r["documentId"] for g in selection["cancelled"] for r in g["copies"]} == {"o", "cn"}


def test_one_system_with_two_numbers_is_two_invoices_and_the_latest_prices_the_deal():
    """RB2: DMS -> DMS with a new number (same VIN) is a cancel-and-reissue,
    never two copies of one bill."""
    original = _vehicle("o", invoice_number="DMS/26/0815", invoice_date="2026-10-10")
    reissue = _vehicle("r", invoice_number="DMS/26/0901", invoice_date="2026-10-15")
    for readings in ([original, reissue], [reissue, original]):
        selection = im.select_invoices(readings)
        assert selection["multipleVehicle"] is True
        assert selection["vehicle"]["representative"]["documentId"] == "r"


def test_a_tally_copy_without_a_vin_pairs_by_total():
    """RS1: DMS with a VIN, Tally without, different numbers, same total."""
    selection = im.select_invoices([_vehicle("dms", invoice_number="DMS/26/0815"),
                                    _tally("tally", invoice_number="TL/2231")])
    assert len(selection["counted"]) == 1 and selection["multipleVehicle"] is False


def test_a_full_credit_note_naming_nothing_is_asked_about_not_a_dealer_discount():
    """RS2: held back with a PC question; never an 8-lakh discount."""
    vehicle = _vehicle("v", invoice_number="DMS/26/0815")
    selection = im.select_invoices([vehicle, _note("cn", narration="Cancellation")])
    assert selection["netPayable"] == Decimal("827200.44")
    assert selection["discounts"]["ADDITIONAL_DISCOUNT"]["amount"] == Decimal("10000.00")  # the invoice's own
    [(note, target)] = selection["cancellationQuestions"]
    assert note["representative"]["documentId"] == "cn" and target["representative"]["documentId"] == "v"


def test_a_credit_note_without_the_tcs_still_cancels_the_invoice():
    """RS3: the credit note gives back the invoice less its TCS."""
    with_tcs = _vehicle("v", invoice_number="DMS/26/0815", tcs_amount="8272.00", grand_total_amount="835472.44")
    selection = im.select_invoices([with_tcs, _note("cn", narration="Reversal of DMS/26/0815")])
    assert selection["vehicle"] is None and selection["netPayable"] is None
    assert len(selection["cancelled"]) == 2


def test_ocr_variants_of_one_invoice_number_are_one_invoice():
    """Live: "INV27O000668" (DMS) vs "INV270000668" (Tally); "INV/27C001498"
    vs "INV27C001498"."""
    for dms_number, tally_number in (("INV27O000668", "INV270000668"), ("INV/27C001498", "INV27C001498")):
        selection = im.select_invoices([_vehicle("d", invoice_number=dms_number),
                                        _tally("t", invoice_number=tally_number, grand_total_amount="827000")])
        assert len(selection["counted"]) == 1, dms_number
        assert selection["counted"][0]["disagreements"], dms_number  # totals differ: flagged, not added


def test_partial_and_decorated_vins_match_the_full_vin():
    """Live: DMS prints "T5H14641" or "T2H36974 (MA1UN2YY2T2H36974)"."""
    partial = _vehicle("d", invoice_number="DMS/1", vin_number="T5H14641")
    full = _tally("t", invoice_number="TL/1", vin_number=VIN, grand_total_amount="827000")
    assert len(im.select_invoices([partial, full])["counted"]) == 1
    decorated = im.read_invoice("customer_invoice_dms", {**EXAMPLE, "vin_number": "T2H36974 (MA1UN2YY2T2H36974)"})
    assert decorated["vinFull"] == "MA1UN2YY2T2H36974" and decorated["vinTail"] == "T2H36974"
    other = _tally("x", invoice_number="TL/2", vin_number="MA1RM2TVXT5H99999")
    assert im.select_invoices([partial, other])["multipleVehicle"] is True


def test_a_year_misread_neither_splits_copies_nor_decides_the_latest_invoice():
    """Live: DMS 2026-09-30 vs Tally 2024-09-30 for one INV27A003006."""
    copies = im.select_invoices([_vehicle("d", invoice_number="INV27A003006", invoice_date="2026-09-30"),
                                 _tally("t", invoice_number="INV27A003006", invoice_date="2024-09-30")])
    assert len(copies["counted"]) == 1
    real = _vehicle("a", invoice_number="DMS/26/0901", invoice_date="2026-10-09")
    misread = _vehicle("b", invoice_number="DMS/26/0815", invoice_date="2028-10-05")
    selection = im.select_invoices([real, misread], plausible_from=date(2026, 9, 20), today=date(2026, 10, 21))
    assert selection["vehicle"]["representative"]["documentId"] == "a"


def test_one_number_with_different_totals_is_copies_that_disagree():
    """Live ADI-BA-261019-001: INV27A003006 DMS 11,33,781 vs 9,99,000."""
    selection = im.select_invoices([
        _vehicle("d1", invoice_number="INV27A003006", grand_total_amount="1133781"),
        _vehicle("d2", invoice_number="INV27A003006", grand_total_amount="999000"),
        _tally("t", invoice_number="INV27A003006", grand_total_amount="999000"),
    ])
    assert len(selection["counted"]) == 1 and selection["multipleVehicle"] is False
    assert any(d["field"] == "netPayable" for d in selection["counted"][0]["disagreements"])


def test_accessory_bills_as_seen_live():
    """UTK-SU-261019-001: DMS C1R27F00000002 (08 Sep, 7,457) and Tally
    C1R27F000000053 (14 Oct, 4,746) are two bills and the PC is asked;
    UTK-SU-261019-002: DMS and Tally both C1R27F000000053 (read once as
    "CIR...") are one bill."""
    def bill(document_id, document_type, number, day, total):
        return im.read_invoice(document_type, {"invoice_number": number, "invoice_date": day,
                                               "grand_total_amount": total}, document_id=document_id)
    two = im.select_invoices([bill("a", "accessory_invoice_dms", "C1R27F00000002", "2026-09-08", "7457"),
                              bill("b", "accessory_invoice_tally", "C1R27F000000053", "2026-10-14", "4746")])
    assert len(two["counted"]) == 2 and len(two["sameBillQuestions"]) == 1
    one = im.select_invoices([bill("c", "accessory_invoice_dms", "CIR27F000000053", "2026-10-14", "4746"),
                              bill("d", "accessory_invoice_tally", "C1R27F000000053", "2026-10-14", "4746")])
    assert len(one["counted"]) == 1 and one["lines"]["accessories_cost"] == Decimal(4746)


def test_grouping_never_marks_the_callers_readings():
    """RN3: the same readings give the same answer twice."""
    readings = [_vehicle("o", invoice_number="DMS/26/0815"), _note("cn", narration="Cancel DMS/26/0815")]
    first, second = im.select_invoices(readings), im.select_invoices(readings)
    assert len(first["cancelled"]) == len(second["cancelled"]) == 2
    assert readings[0]["cancelled"] is None


def test_a_voided_booking_stage_invoice_leaves_the_canonical_tables_on_rematerialisation(journey):
    """The Booking-stage durable pass re-derives the invoice actuals too, so
    a voided Booking invoice stops counting even when it was the stage's
    only document."""
    from audit_core.uc03_delivery_post_extraction_materialization import (
        materialize_booking_documents_from_durable_store,
    )

    invoice = add_ready_document(journey, "customer_invoice_dms", **EXAMPLE)
    _materialize(journey)
    assert _line(journey, "ex_showroom_price")["actual_amount"] == Decimal("852000.00")

    _sql(journey, "UPDATE auditcore.evidence SET association_status='VOIDED' WHERE tenant_id=:t AND "
                  "di_document_id=:d", d=invoice)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        result = materialize_booking_documents_from_durable_store(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert not result.get("error"), result
    line = _line(journey, "ex_showroom_price")
    assert line["actual_amount"] is None and line["source_reference"] is None
    discounts = _sql(journey, "SELECT actual_discount_amount, details->>'origin' FROM auditcore.discount_applications "
                              "WHERE tenant_id=:t AND journey_id=:j").all()
    assert discounts and all(a is None and o == "INVOICE_WITHDRAWN" for a, o in discounts)


# ── final check of PR #470 (2026-10-21) ──────────────────────────────────────
def _both_passes(journey):
    from audit_core.uc03_delivery_post_extraction_materialization import (
        materialize_booking_documents_from_durable_store,
    )

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        im.sync_invoice_actuals(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                stage_code="DELIVERY")
        result = materialize_booking_documents_from_durable_store(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert not result.get("error"), result


def test_one_invoice_problem_is_one_finding_and_one_task_across_both_passes(journey):
    """FB1: the Booking and Delivery passes both re-check the invoices; the
    problem is the journey's, raised once. A duplicate raised earlier under
    the other stage key is closed (its task cancelled)."""
    add_ready_document(journey, "customer_invoice_dms", **{**EXAMPLE, "vin_number": "MA1VEHICLE0000001"})
    add_ready_document(journey, "customer_invoice_dms", **{**EXAMPLE, "invoice_number": "DMS/26/0901",
                                                           "vin_number": "MA1VEHICLE0000002"})
    _materialize(journey)  # a BOOKING-stage confirm
    _both_passes(journey)
    _both_passes(journey)
    findings = _sql(journey, "SELECT stage_code, finding_status FROM auditcore.audit_findings WHERE tenant_id=:t "
                             "AND journey_id=:j AND rule_key='MULTIPLE_VEHICLE_INVOICES' "
                             "AND finding_status IN ('OPEN','ACKNOWLEDGED')").all()
    assert findings == [("DELIVERY", "OPEN")]
    tasks = _sql(journey, "SELECT count(*) FROM auditcore.workflow_tasks WHERE tenant_id=:t AND journey_id=:j "
                          "AND task_type='INVOICE_DISCREPANCY_REVIEW' "
                          "AND task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT')").scalar_one()
    assert tasks == 1


def test_a_value_written_for_an_invoice_on_file_is_stable_across_passes(journey):
    """FS1: an invoice-held value with no review row yet (written for an
    invoice not yet confirmed) stays put while that document is on file --
    no flip-flop with the booking form on each pass."""
    from audit_core.uc03_deal_source_history import upsert_commercial_actual

    booking = add_ready_document(journey, "booking_form", tcs_amount="0")
    invoice = add_ready_document(journey, "customer_invoice_dms", invoice_number="DMS/26/0815")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        upsert_commercial_actual(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                 component_key="tcs_amount", amount=Decimal(0), document_type="booking_form",
                                 document_id=booking, evidence_id=None)
        upsert_commercial_actual(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                 component_key="tcs_amount", amount=Decimal("8272.00"),
                                 document_type="customer_invoice_dms", document_id=invoice, evidence_id=None)
    for _ in range(2):
        _both_passes(journey)
        line = _line(journey, "tcs_amount")
        assert line["actual_amount"] == Decimal("8272.00")
        assert line["source_reference"] == f"customer_invoice_dms:{invoice}"


def test_same_system_numbers_equal_only_after_ocr_with_different_totals_are_two_invoices():
    """FN2: "CLR/26/0091" and "C1R/26/0091" from DMS with different totals
    are two bills; the very same number printed twice is one bill."""
    def bill(document_id, number, total):
        return im.read_invoice("accessory_invoice_dms", {"invoice_number": number, "grand_total_amount": total},
                               document_id=document_id)
    two = im.select_invoices([bill("a", "CLR/26/0091", "7457.01"), bill("b", "C1R/26/0091", "2500")])
    assert len(two["counted"]) == 2 and two["lines"]["accessories_cost"] == Decimal("9957.01")
    assert len({g["key"] for g in two["groups"]}) == 2
    same = im.select_invoices([bill("c", "C1R/26/0091", "7457.01"), bill("d", "C1R/26/0091", "2500")])
    assert len(same["counted"]) == 1 and same["counted"][0]["disagreements"]
    vehicles = im.select_invoices([_vehicle("v1", invoice_number="INV27O000668", vin_number=None),
                                   _vehicle("v2", invoice_number="INV270000668", vin_number=None,
                                            grand_total_amount="999000")])
    assert vehicles["multipleVehicle"] is True


def test_a_credit_note_names_an_invoice_only_by_its_whole_number():
    """FN3: "101" inside other text does not name invoice 101; a short
    number never names an invoice on its own."""
    target = _vehicle("v", invoice_number="DMS/26/0815")
    loose = _note("cn", narration="Refund ref 26/08150 against booking")  # a longer number, not 0815's
    assert im.select_invoices([target, loose])["cancellationQuestions"]  # asked, not cancelled
    labelled = _note("cn2", narration="Cancel No.DMS/26/0815")
    assert len(im.select_invoices([target, labelled])["cancelled"]) == 2
    short = _vehicle("s", invoice_number="101")
    assert im.select_invoices([short, _note("cn3", narration="Cancel invoice 101")])["cancellationQuestions"]
