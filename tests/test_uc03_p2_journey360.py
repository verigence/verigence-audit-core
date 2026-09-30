"""P2 Journey 360: deal side-by-side, add-ons, all extracted fields, ETag."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_journey360 import component_category, deal


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2j360")
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _source(connection, journey, kind, key, document_type, amount):
    connection.execute(
        text(
            """
            INSERT INTO auditcore.commercial_line_source_values (
                tenant_id, journey_id, line_kind, component_key, source_document_type, amount, source_document_id
            ) VALUES (:t, :j, :k, :c, :d, :a, :doc)
            """
        ),
        {"t": journey.tenant_id, "j": journey.journey_id, "k": kind, "c": key, "d": document_type,
         "a": amount, "doc": uuid4()},
    )


def _seed_deal(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        for key, amount in (("ex_showroom_price", "1000000"), ("insurance_amount", "40000"),
                            ("essential_kit_amount", "15000")):
            _source(connection, journey, "COMMERCIAL", key, "booking_form", amount)
        _source(connection, journey, "COMMERCIAL", "ex_showroom_price", "tax_invoice_tally", "1005000")
        _source(connection, journey, "COMMERCIAL", "insurance_amount", "insurance_cover", "40000")
        _source(connection, journey, "COMMERCIAL", "ex_showroom_price", "customer_ledger", "1005000")
        _source(connection, journey, "DISCOUNT", "exchange_discount_amount", "booking_form", "30000")
        _source(connection, journey, "DISCOUNT", "CASH_DISCOUNT", "tax_invoice_tally", "20000")
        connection.execute(
            text(
                """
                INSERT INTO auditcore.discount_applications (
                    tenant_id, journey_id, discount_key, standard_eligible_amount, actual_discount_amount,
                    eligibility_result, actual_source_kind
                ) VALUES (:t, :j, 'CASH_DISCOUNT', 25000, 20000, 'ELIGIBLE', 'EVIDENCE')
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )


def test_component_categories():
    assert component_category("ex_showroom_price") == "VEHICLE"
    assert component_category("road_tax_amount") == "REGISTRATION"
    assert component_category("genuine_accessories_amount") == "ACCESSORIES"
    assert component_category("additional_warranty_amount") == "PROTECTION"
    assert component_category("insurance_amount") == "INSURANCE"
    assert component_category("fastag_amount") == "OTHER"


def test_deal_lays_out_standard_booking_billed_ledger_and_variance(journey):
    _seed_deal(journey)
    add_receipt_payment(journey, amount="50000", receipt_number="R1", receipt_date="2026-09-01")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        view = deal(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)

    groups = {g["code"]: g for g in view["categories"]}
    assert list(groups) == ["VEHICLE", "INSURANCE", "ACCESSORIES"]
    [price] = groups["VEHICLE"]["components"]
    assert Decimal(price["booking"]) == 1000000 and Decimal(price["billed"]) == 1005000
    assert Decimal(price["ledger"]) == 1005000
    assert Decimal(price["bookingVsBilled"]) == 5000
    assert "BOOKING_VS_BILLED" in price["flags"]
    [insurance] = groups["INSURANCE"]["components"]
    assert insurance["flags"] == []

    discounts = {d["key"]: d for d in view["discounts"]}
    # Booking-form field names fold onto the canonical scheme benefit.
    assert Decimal(discounts["EXCHANGE_BONUS"]["booking"]) == 30000
    assert discounts["EXCHANGE_BONUS"]["proof"]["documentType"] == "vehicle_rc"
    assert discounts["EXCHANGE_BONUS"]["proof"]["onFile"] is False
    cash = discounts["CASH_DISCOUNT"]
    assert Decimal(cash["entitled"]) == 25000 and Decimal(cash["billed"]) == 20000
    assert Decimal(cash["variance"]) == -5000 and "UNDER_ENTITLEMENT" in cash["flags"]

    summary = view["summary"]
    assert Decimal(summary["gross"]["booking"]) == 1055000
    assert Decimal(summary["net"]["booking"]) == 1025000
    assert Decimal(summary["paid"]["receipts"]) == 50000
    assert Decimal(summary["balanceDue"]) == Decimal(summary["payable"]) - 50000


def test_summary_etag_and_sections(journey):
    _seed_deal(journey)
    document_id = add_ready_document(journey, "pan_card", pan_number="ABCDE1234F", pan_name="P2 CUSTOMER")
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/360"

    first = client.get(base)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["journey"]["customerName"] == "P2 Customer"
    assert body["numbers"]["documents"] == 1
    assert "deal" in body["sections"] and "documents" in body["sections"]
    etag = first.headers["etag"]
    assert client.get(base, headers={"If-None-Match": etag}).status_code == 304

    documents = client.get(f"{base}/documents").json()["documents"]
    [pan] = [d for d in documents if d["documentId"] == str(document_id)]
    assert pan["label"] == "PAN Card" or pan["templateKey"] == "pan_card"
    assert {f["key"] for f in pan["fields"]} == {"pan_number", "pan_name"}

    for section in ("deal", "addons", "payments", "vehicle", "registration", "delivery", "compliance", "activity", "timeline"):
        response = client.get(f"{base}/{section}")
        assert response.status_code == 200, (section, response.text)
    assert "timeline" not in body["sections"] and "activity" not in body["sections"] and "delivery" not in body["sections"]

    # The Customer tab: what was entered, what the KYC says, contact masked.
    customer = client.get(f"{base}/customer").json()
    by_key = {f["key"]: f for f in customer["fields"]}
    assert [f["label"] for f in customer["fields"]][:3] == ["Entered name", "Legal / KYC name", "PAN"]
    assert by_key["enteredName"]["value"] == "P2 Customer"
    assert by_key["legalName"]["value"] == "P2 CUSTOMER" and by_key["legalName"]["source"] == "pan_card"
    assert by_key["pan"]["value"] == "ABCDE1234F"
    assert by_key["customerType"]["value"] == "INDIVIDUAL"
    assert customer["identityStatus"] == "DOCUMENT_VERIFIED" and by_key["identityStatus"]["value"] == "DOCUMENT_VERIFIED"
    assert customer["kycDocuments"] == ["pan_card"]
    assert client.get(f"{base}/nope").status_code == 404

    # The audit trail: milestones in order with the hours between them, the
    # tasks with when they opened and closed, and one ordered event stream.
    audit = client.get(f"{base}/audit").json()
    assert audit["milestones"][0]["key"] == "JOURNEY_STARTED" and audit["milestones"][0]["hoursSincePrevious"] is None
    assert all(m["atUtc"] for m in audit["milestones"])
    assert {"opened", "closed", "open", "avgHoursToClose"} <= set(audit["tasks"]["summary"])
    assert audit["events"] == sorted(audit["events"], key=lambda e: e["atUtc"])
    # How each stage completed: the gates with their outcome, the rules fired.
    booking = audit["completion"]["booking"]
    assert [g["key"] for g in booking["gates"]] == ["BOOKING_FORM_EXTRACTED", "KYC_EXTRACTED", "MINIMUM_BOOKING_PAYMENT"]
    assert all(g["status"] in ("WAITING", "PASS", "FAIL") and g["label"] for g in booking["gates"])
    assert set(booking["counts"]) == {"fired", "passed", "failed", "waiting"}
    assert audit["completion"]["delivery"]["gates"]
    assert all({"atUtc", "kind", "type", "who"} <= set(e) for e in audit["events"])
    assert set(audit["stages"]) == {"BOOKING", "DELIVERY"} and isinstance(audit["roles"], list)

    # Trade-in / Scrappage and the Vehicle panel as Phase 1 lays them out.
    add_ready_document(journey, "booking_form", customer_name="P2 CUSTOMER", exchange_applicable=True,
                       exchange_value="150000", sales_person="K K SATHAPATHI", dealer_branch="Jajpur",
                       booking_date="2026-08-31", expected_delivery_date="2026-09-30", accessories_cost="61308.02")
    cod = add_ready_document(journey, "scrappage_certificate_of_deposit", certificate_number="COD2026091HR26AW8810",
                             certificate_variant="Transfer Certificate of Deposit", old_vehicle_registration_number="HR26AW8810",
                             old_vehicle_make="HYUNDAI MOTOR INDIA LTD", old_vehicle_model="SANTRO",
                             current_holder_name="P2 CUSTOMER", trade_date="2026-09-08")
    add_ready_document(journey, "accessory_invoice_dms", invoice_number="ACC-1", buyer_name="P2 CUSTOMER",
                       grand_total_amount="61308.02", line_items=[
                           {"description_raw": "Roof Rail Set - Scorpio", "line_category": "ACCESSORY_GENUINE",
                            "quantity": 1, "net_amount": 4917},
                           {"description_raw": "Dual USB Car Charger", "line_category": "ACCESSORY_NON_GENUINE",
                            "net_amount": "647.01"},
                           {"description_raw": "CGST", "line_category": "TAX_LINE", "net_amount": 100},
                       ])
    trade = client.get(f"{base}/tradein").json()
    assert trade["exchange"] == {"applicable": True, "value": "150000"}
    assert trade["tradeIn"] is None
    [certificate] = trade["certificates"]
    assert certificate["documentId"] == str(cod)
    assert certificate["certificateNumber"] == "COD2026091HR26AW8810"
    assert certificate["oldVehicleModel"] == "SANTRO" and certificate["currentHolderName"] == "P2 CUSTOMER"
    assert certificate["tradeDate"] == "2026-09-08" and certificate["scrappingFacilityName"] is None

    vehicle = client.get(f"{base}/vehicle").json()
    assert vehicle["addons"]["accessories"]["taken"] is True
    assert [(i["name"], i["amount"]) for i in vehicle["addons"]["accessories"]["items"]] == [
        ("Roof Rail Set - Scorpio", "4917"), ("Dual USB Car Charger", "647.01"),
    ]
    assert vehicle["addons"]["warranty"] == {"taken": False, "amount": None, "amountSource": None, "provider": None,
                                             "items": [], "details": {}, "documentIds": []}
    assert vehicle["addons"]["accessories"]["details"] == {"invoiceNumbers": ["ACC-1"]}
    # The accessory invoice is the billed value: the same answer in the Deal,
    # the Add-ons tab and the Vehicle tab.
    assert vehicle["addons"]["accessories"]["amount"] == "61308.02"
    assert vehicle["addons"]["accessories"]["amountSource"] == "billed"
    deal_view = client.get(f"{base}/deal").json()
    accessories = next(r for g in deal_view["categories"] for r in g["components"] if r["key"] == "accessories_cost")
    assert accessories["billed"] == "61308.02" and accessories["booking"] == "61308.02"
    assert [i["label"] for i in deal_view["invoices"]] and deal_view["invoices"][0]["number"] == "ACC-1"
    assert client.get(f"{base}/addons").json()["taken"]["accessories"]["taken"] is True

    # Every invoice with its header, totals and line items as printed.
    listing = client.get(f"{base}/invoices").json()
    assert listing["count"] == 1
    [accessory] = listing["documents"]
    assert accessory["documentType"] == "accessory_invoice_dms" and accessory["label"]
    assert accessory["header"]["invoiceNumber"] == "ACC-1" and accessory["header"]["buyerName"] == "P2 CUSTOMER"
    assert accessory["totals"]["grandTotalAmount"] == "61308.02"
    assert [(l["description"], l["category"], l["netAmount"]) for l in accessory["lineItems"]] == [
        ("Roof Rail Set - Scorpio", "ACCESSORY_GENUINE", "4917"), ("Dual USB Car Charger", "ACCESSORY_NON_GENUINE", "647.01"),
        ("CGST", "TAX_LINE", "100"),
    ]
    assert vehicle["booking"]["salesConsultant"] == "K K SATHAPATHI"
    assert vehicle["booking"]["dealerBranch"] == "Jajpur"
    assert vehicle["booking"]["bookingDate"] == "2026-08-31"
    assert vehicle["booking"]["expectedDelivery"] == "2026-09-30"
    assert vehicle["delivery"] is None

    # A new fact changes the ETag.
    add_ready_document(journey, "aadhaar", aadhaar_number="123412341234")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        from audit_core.uc03_p2_runtime import note_facts_changed
        note_facts_changed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, reason="t")
    assert client.get(base, headers={"If-None-Match": etag}).status_code == 200


def test_compliance_report_extends_legacy_with_ledger_and_verdict(journey):
    _seed_deal(journey)
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/360"
    report = client.get(f"{base}/compliance-report")
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["header"]["journeyId"] == str(journey.journey_id)
    assert body["verdict"]["code"] in {"INCOMPLETE", "NON_COMPLIANT"}
    # Printable by anyone who can open the Journey, but a draft until the TL reviews the delivery.
    assert body["review"]["status"] == "DRAFT" and body["review"]["label"].startswith("Draft")
    assert "BOOKING" in body["controls"] and body["controls"]["BOOKING"]
    labels = {line["label"] for line in body["deal"]["flaggedLines"]}
    assert "Ex-showroom price" in labels
    assert client.get(f"{base}/duplicates").json() == {"pairs": []}


def test_partly_invoiced_deal_is_not_reported_short(journey):
    """A component not billed yet keeps its booking value in the current
    total, and variances compare only lines present on both sides."""
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text(
                """
                INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount, actual_amount)
                VALUES (:t, :j, 'ex_showroom_price', 1000000, 1000000), (:t, :j, 'registration_charges', 90000, 90000)
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
        _source(connection, journey, "COMMERCIAL", "ex_showroom_price", "booking_form", "1000000")
        _source(connection, journey, "COMMERCIAL", "registration_charges", "booking_form", "90000")
        _source(connection, journey, "COMMERCIAL", "ex_showroom_price", "tax_invoice_tally", "1000000")
        summary = deal(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)["summary"]
    assert Decimal(summary["net"]["current"]) == 1090000
    assert Decimal(summary["variance"]["currentVsStandard"]) == 0
    assert Decimal(summary["variance"]["billedVsBooking"]) == 0
    assert summary["invoicedComponents"] == 1 and summary["components"] == 2


def test_discount_without_entitlement_is_an_over_grant(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text(
                """
                INSERT INTO auditcore.discount_applications (
                    tenant_id, journey_id, discount_key, standard_eligible_amount, actual_discount_amount,
                    eligibility_result, actual_source_kind
                ) VALUES (:t, :j, 'CORPORATE_PRIVILEGE', NULL, 5000, 'NOT_ELIGIBLE', 'EVIDENCE')
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
        _source(connection, journey, "DISCOUNT", "CORPORATE_PRIVILEGE", "booking_form", "5000")
        [row] = deal(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)["discounts"]
    assert Decimal(row["entitled"]) == 0 and Decimal(row["variance"]) == 5000
    assert "OVER_ENTITLEMENT" in row["flags"]


def test_insurance_taken_reads_the_cover_note_not_the_bookings_zero(journey):
    """The booking form prices insurance at 0 (customer to arrange), then a
    cover note arrives: the policy exists, the premium is what the cover
    bills, and every tab says so."""
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        _source(connection, journey, "COMMERCIAL", "insurance_amount", "booking_form", "0")
        connection.execute(
            text(
                """
                INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount,
                    actual_amount, actual_source_kind, source_reference)
                VALUES (:t, :j, 'insurance_amount', 71809, 0, 'EVIDENCE', 'booking_form:x')
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
    cover = add_ready_document(journey, "insurance_cover", insurer_name="Zurich Kotak", policy_number="MKG/P30598174",
                               premium_amount="62,029", add_ons=["CONSUMABLES COVER", "ENGINE PROTECT"])
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/360"
    vehicle = client.get(f"{base}/vehicle").json()
    insurance = vehicle["addons"]["insurance"]
    assert insurance["taken"] is True and insurance["amount"] == "62029" and insurance["amountSource"] == "billed"
    assert insurance["provider"] == "Zurich Kotak" and insurance["documentIds"] == [str(cover)]
    assert insurance["details"]["policyNumber"] == "MKG/P30598174"
    assert client.get(f"{base}/addons").json()["taken"]["insurance"]["taken"] is True
    row = next(r for g in client.get(f"{base}/deal").json()["categories"] for r in g["components"]
               if r["key"] == "insurance_amount")
    assert Decimal(row["booking"]) == 0 and row["billed"] == "62029" and Decimal(row["variance"]) == -9780


def test_duplicate_receipts_count_once_and_receipts_meet_the_bank_statement(journey):
    add_receipt_payment(journey, amount="84308", receipt_number="AMP-B/04824/26-27", receipt_date="2026-09-11")
    add_receipt_payment(journey, amount="84308", receipt_number="AMP-B/04824/26-27", receipt_date="2026-09-11")
    add_receipt_payment(journey, amount="21000", receipt_number="AMP-B/04001/26-27", receipt_date="2026-09-01")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.payments SET payment_reference='UTR526612345678', payment_method_code='NEFT' "
                 "WHERE tenant_id=:t AND receipt_number='AMP-B/04824/26-27'"),
            {"t": journey.tenant_id},
        )
    statement = add_ready_document(journey, "bank_statement_extract", bank_name="HDFC Bank", transaction_date="12/09/2026",
                                   transaction_description="NEFT CR UTR526612345678 BISWABHANU", reference_no="526612345678",
                                   credit_amount="84,308.00", running_balance="1,20,000")
    add_ready_document(journey, "bank_statement_extract", bank_name="HDFC Bank", transaction_date="2026-09-20",
                       transaction_description="CASH DEP", credit_amount="5000")
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/360"
    view = client.get(f"{base}/payments").json()
    by_number = {}
    for item in view["items"]:
        by_number.setdefault(item["receiptNumber"], []).append(item)
    first, second = by_number["AMP-B/04824/26-27"]
    assert first["counted"] is True and second["counted"] is False
    assert second["duplicateOf"] == first["paymentId"]
    assert "same receipt number, amount and date" in second["notCountedReason"]
    assert view["duplicates"] == 1
    assert Decimal(view["receiptsTotal"]) == 105308  # 84308 once, plus 21000
    assert Decimal(client.get(base).json()["money"]["paid"]["receipts"]) == 105308
    # The counted receipt meets its statement credit by UTR; the duplicate gets nothing.
    assert first["bankStatement"]["status"] == "MATCHED" and first["bankStatement"]["method"] == "UTR"
    assert first["bankStatement"]["documentId"] == str(statement)
    assert second["bankStatement"] is None
    (other,) = by_number["AMP-B/04001/26-27"]
    assert other["bankStatement"]["status"] == "UNMATCHED"
    bank = view["bankStatement"]
    assert Decimal(bank["creditsTotal"]) == 89308 and Decimal(bank["matchedTotal"]) == 84308
    assert bank["matched"] == 1 and bank["unmatchedCredits"] == 1 and bank["receiptsWithoutCredit"] == 1
    matched = next(line for line in bank["lines"] if line["matchedPaymentId"])
    assert matched["matchedReceipt"] == "AMP-B/04824/26-27" and matched["date"] == "2026-09-12"


def test_one_certificate_however_many_times_it_was_read(journey):
    full = add_ready_document(journey, "scrappage_certificate_of_deposit", certificate_number="COD2026091HR26AW8810",
                              trade_number="33080926232094421528", old_vehicle_registration_number="HR26AW8810",
                              old_vehicle_model="SANTRO", current_holder_name="P2 CUSTOMER", trade_date="2026-09-08")
    partial = add_ready_document(journey, "scrappage_certificate_of_deposit", trade_number="33080926232094421528",
                                 current_holder_name="P2 CUSTOMER", state_of_scrapping="Haryana")
    add_ready_document(journey, "scrappage_certificate_of_deposit", certificate_number="COD-OTHER", old_vehicle_model="ALTO")
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/360"
    certificates = client.get(f"{base}/tradein").json()["certificates"]
    assert len(certificates) == 2
    merged = next(c for c in certificates if c["tradeNumber"] == "33080926232094421528")
    assert set(merged["documentIds"]) == {str(full), str(partial)}
    assert merged["certificateNumber"] == "COD2026091HR26AW8810" and merged["oldVehicleModel"] == "SANTRO"
    assert merged["stateOfScrapping"] == "Haryana"  # from the partial reading


def test_documents_never_ask_to_verify_an_empty_field(journey):
    """Reported live (2026-09-30): the Documents tab said "To verify" on every
    field the extractor left blank (Booking date, Total price, ...). Only a
    value that was read, below the 90% bar, needs a look -- the same rule the
    review tasks apply (uc03_p2_stage.unreviewed_fields)."""
    from p2_support import add_evidence, add_extracted_field

    from audit_core.uc03_p2_journey360 import documents

    document_id = uuid4()
    add_evidence(journey, di_document_id=document_id, document_type_key="pan_card")
    add_extracted_field(journey, di_document_id=document_id, field_key="pan_number", value="ABCDE1234F",
                        confidence=92.0, document_type="pan_card")
    add_extracted_field(journey, di_document_id=document_id, field_key="pan_name", value="Sujata",
                        confidence=60.0, document_type="pan_card")
    add_extracted_field(journey, di_document_id=document_id, field_key="date_of_birth", value=None,
                        confidence=None, document_type="pan_card")
    add_extracted_field(journey, di_document_id=document_id, field_key="father_name", value="",
                        confidence=None, document_type="pan_card")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        view = documents(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)

    [document] = view["documents"]
    by_key = {f["key"]: f for f in document["fields"]}
    assert by_key["pan_number"]["needsReview"] is False
    assert by_key["pan_name"]["needsReview"] is True
    assert by_key["date_of_birth"]["needsReview"] is False
    assert by_key["father_name"]["needsReview"] is False
    assert document["needsReview"] == 1


def test_vehicle_section_carries_what_the_header_strip_used_to(journey):
    """Issue 10 (2026-09-30): the Journey 360 header strip was removed as a
    duplicate; registration, financier, insurer and the start date now live
    on the Vehicle tab."""
    from audit_core.uc03_p2_journey360 import vehicle

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.finance_records (tenant_id, journey_id, provider_name) VALUES (:t, :j, 'HDFC Bank')"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
        view = vehicle(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert view["journey"]["financier"] == "HDFC Bank"
    assert view["journey"]["startedAtUtc"] is not None
    assert view["journey"]["registrationNumber"] is None and view["journey"]["insurer"] is None


def test_opted_lines_read_the_invoice_once_one_exists_else_the_booking_form(journey):
    """Decision 2026-09-30: the Deal tab shows what the customer opted for
    on the discounts, accessories and extended warranty. Until an invoice
    is read the booking form says; once one is, the invoice says and a
    line it does not carry is opted out."""
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        _source(connection, journey, "COMMERCIAL", "essential_kit_amount", "booking_form", "15000")
        _source(connection, journey, "COMMERCIAL", "additional_warranty_amount", "booking_form", "0")
        _source(connection, journey, "DISCOUNT", "CASH_DISCOUNT", "booking_form", "20000")
        view = deal(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    rows = {r["key"]: r for g in view["categories"] for r in g["components"]}
    assert rows["essential_kit_amount"]["opted"] == {"taken": True, "source": "booking"}
    assert rows["additional_warranty_amount"]["opted"] == {"taken": False, "source": "booking"}
    assert view["discounts"][0]["opted"] == {"taken": True, "source": "booking"}
    assert view["insurance"] == {"source": "INHOUSE", "decidedBy": "DEFAULT", "decidedAt": None, "actorId": None,
                                 "invoiceOnFile": False, "vehicleInvoiced": False}

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        _source(connection, journey, "COMMERCIAL", "ex_showroom_price", "tax_invoice_tally", "1005000")
        _source(connection, journey, "COMMERCIAL", "additional_warranty_amount", "ew_invoice", "12000")
        view = deal(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    rows = {r["key"]: r for g in view["categories"] for r in g["components"]}
    assert rows["ex_showroom_price"].get("opted") is None  # the vehicle price is never opted out
    assert rows["essential_kit_amount"]["opted"] == {"taken": False, "source": "invoice"}
    assert rows["additional_warranty_amount"]["opted"] == {"taken": True, "source": "invoice"}
    assert view["discounts"][0]["opted"] == {"taken": False, "source": "invoice"}


def test_self_insurance_keeps_the_premium_out_of_every_total(journey):
    from audit_core.uc03_p2_deal_actions import set_insurance_source

    _seed_deal(journey)
    add_ready_document(journey, "tax_invoice_tally", invoice_number="INV-1")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        before = deal(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        set_insurance_source(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, source="SELF",
                             actor_id=journey.actor_id, reason="Customer brought own policy", correlation_id="c",
                             via="DEAL_TAB")
        after = deal(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert before["insurance"]["source"] == "INHOUSE" and before["insurance"]["vehicleInvoiced"] is True
    assert before["insurance"]["invoiceOnFile"] is False  # the cover note is not an invoice
    assert Decimal(before["summary"]["gross"]["booking"]) == 1055000 and before["summary"]["components"] == 3
    assert after["insurance"]["source"] == "SELF" and after["insurance"]["decidedBy"] == "PC"
    insurance = next(g for g in after["categories"] if g["code"] == "INSURANCE")
    assert insurance["excluded"] is True and insurance["components"][0]["excluded"] is True
    assert Decimal(insurance["components"][0]["billed"]) == 40000  # still shown
    assert Decimal(after["summary"]["gross"]["booking"]) == 1015000 and after["summary"]["components"] == 2
    assert Decimal(after["summary"]["net"]["current"]) == Decimal(before["summary"]["net"]["current"]) - 40000
    assert Decimal(after["summary"]["payable"]) == Decimal(before["summary"]["payable"]) - 40000
