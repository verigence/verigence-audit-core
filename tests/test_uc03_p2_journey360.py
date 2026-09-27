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

    for section in ("deal", "addons", "payments", "vehicle", "registration", "delivery", "compliance", "activity"):
        response = client.get(f"{base}/{section}")
        assert response.status_code == 200, (section, response.text)
    assert client.get(f"{base}/nope").status_code == 404

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
    assert "BOOKING" in body["controls"] and body["controls"]["BOOKING"]
    labels = {line["label"] for line in body["deal"]["flaggedLines"]}
    assert "Ex-showroom price" in labels
    assert client.get(f"{base}/duplicates").json() == {"pairs": []}
