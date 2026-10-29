"""Audit summary of a Journey (product owner 2026-10-29): every financial fact
on one page, each with a conclusion from a fixed rule, Rs 10 either way being
no difference."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_evidence,
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_p2_audit_summary as summary
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_masters_alignment import ENTITLEMENT_UNKNOWN_RESULTS


def _src(document_type: str, amount: str, label: str | None = None) -> dict:
    return {"documentType": document_type, "document": label or document_type, "amount": amount}


# ── the rules (no database) ──────────────────────────────────────────────────
def test_a_charge_within_rs_10_of_the_price_master_is_ok_and_beyond_it_is_an_exception() -> None:
    row = {"key": "ex_showroom_price", "label": "Ex-showroom price", "standard": "1000000",
           "billed": "1000008", "sources": [_src("customer_invoice_dms", "1000008")]}
    assert summary.conclude_charge(row, on_file=set())["conclusion"] == summary.OK
    above = summary.conclude_charge({**row, "billed": "1000500", "sources": [_src("customer_invoice_dms", "1000500")]},
                                    on_file=set())
    assert (above["conclusion"], above["impact"], above["difference"], above["rule"]) == \
        (summary.EXCEPTION, summary.CUSTOMER, "500.00", "AS-01")
    below = summary.conclude_charge({**row, "billed": "999000", "sources": [_src("customer_invoice_dms", "999000")]},
                                    on_file=set())
    assert (below["conclusion"], below["impact"]) == (summary.EXCEPTION, summary.COMPANY)


def test_a_blank_deal_line_takes_the_first_document_that_prints_it() -> None:
    # ADI-PB-261022-001: no NDC, the cost sheet has no TCS; the invoice prints it.
    row = {"key": "tcs_amount", "label": "TCS", "standard": "16899", "billed": None,
           "sources": [_src("customer_invoice_dms", "16899", "DMS invoice"), _src("booking_docket", "17399")]}
    out = summary.conclude_charge(row, on_file=set())
    assert (out["billed"], out["billedFrom"], out["conclusion"]) == ("16899.00", "DMS invoice", summary.OK)


def test_documents_that_print_different_figures_are_disputed() -> None:
    # Registration: the customer ledger Rs 10,000 above the RTO challan.
    row = {"key": "road_tax_registration", "label": "Road tax + registration", "standard": None, "billed": "132132",
           "sources": [_src("cost_sheet", "132132"), _src("customer_ledger", "142132"), _src("rto_challan", "132132")]}
    out = summary.conclude_charge(row, on_file={"rto_challan"})
    assert out["conclusion"] == summary.DISPUTED and out["rule"] == "AS-03"
    assert {d["amount"] for d in out["documents"]} == {"132132.00", "142132.00"}


def test_a_statutory_line_is_measured_against_its_proof_document() -> None:
    row = {"key": "insurance_amount", "label": "Insurance premium", "standard": "84180", "billed": "74827",
           "sources": [_src("cost_sheet", "74827"), _src("insurance_cover", "74827")]}
    out = summary.conclude_charge(row, on_file={"insurance_cover"})
    # Not an exception against the price master's estimate: the policy agrees.
    assert (out["conclusion"], out["rule"], out["proof"]["onFile"]) == (summary.OK, "AS-02", True)
    no_policy = summary.conclude_charge({**row, "sources": [_src("cost_sheet", "74827")]}, on_file=set())
    assert no_policy["conclusion"] == summary.UNSUPPORTED


def test_a_charge_with_no_price_master_cannot_be_concluded() -> None:
    out = summary.conclude_charge({"key": "fastag_amount", "label": "FASTag", "standard": None, "billed": "500",
                                   "sources": [_src("customer_ledger", "500")]}, on_file=set())
    assert out["conclusion"] == summary.CANNOT and "price master" in out["reason"]


def test_benefits_against_entitlement_and_proof() -> None:
    base = {"key": "CASH_DISCOUNT", "label": "Consumer / cash discount", "entitled": "25000",
            "eligibility": "ELIGIBLE", "proof": None}
    unknown = ENTITLEMENT_UNKNOWN_RESULTS
    ok = summary.conclude_benefit({**base, "billed": "25005", "sources": [_src("customer_invoice_dms", "25005")]},
                                  unknown_results=unknown)
    assert ok["conclusion"] == summary.OK
    over = summary.conclude_benefit({**base, "billed": "35000", "sources": [_src("customer_invoice_dms", "35000")]},
                                    unknown_results=unknown)
    assert (over["conclusion"], over["impact"], over["difference"]) == (summary.EXCEPTION, summary.COMPANY, "10000.00")
    under = summary.conclude_benefit({**base, "billed": "20000", "sources": [_src("customer_invoice_dms", "20000")]},
                                     unknown_results=unknown)
    assert (under["conclusion"], under["impact"]) == (summary.EXCEPTION, summary.CUSTOMER)
    not_known = summary.conclude_benefit({**base, "entitled": None, "eligibility": "NOT_EVALUATED", "billed": "20000",
                                          "sources": []}, unknown_results=unknown)
    assert not_known["conclusion"] == summary.CANNOT and "not evaluated" in not_known["reason"]
    no_rc = summary.conclude_benefit({**base, "key": "EXCHANGE_BONUS", "label": "Exchange bonus", "billed": "35000",
                                      "proof": {"documentType": "vehicle_rc", "document": "Vehicle RC", "onFile": False},
                                      "sources": []}, unknown_results=unknown)
    assert (no_rc["conclusion"], no_rc["rule"]) == (summary.UNSUPPORTED, "AS-05")
    not_given = summary.conclude_benefit({**base, "billed": None, "effective": None, "sources": []},
                                         unknown_results=unknown)
    assert (not_given["conclusion"], not_given["reason"]) == (summary.INFO, "Entitled, not given.")


def test_a_product_charged_without_its_document_is_unsupported() -> None:
    charge = summary.conclude_charge({"key": "additional_warranty_amount", "label": "Extended warranty",
                                      "standard": "17999", "billed": "17999",
                                      "sources": [_src("cost_sheet", "17999")]}, on_file=set())
    out = summary.conclude_product(charge, on_file=set(), taken=None)
    assert (out["conclusion"], out["rule"], out["taken"]) == (summary.UNSUPPORTED, "AS-06", True)
    with_invoice = summary.conclude_product(charge, on_file={"ew_invoice"}, taken=None)
    assert with_invoice["conclusion"] == summary.OK


def test_settlement_and_verdict() -> None:
    assert summary.settlement(Decimal(1950691), Decimal(1950685))["status"] == "SETTLED"  # Rs 6: no difference
    # ADI-PB-261022-001: Rs 26 short is beyond the Rs 10 either way.
    assert summary.settlement(Decimal(1950691), Decimal(1950665))["status"] == "SHORT"
    short = summary.settlement(Decimal(100000), Decimal(90000))
    assert (short["status"], short["balance"], short["conclusion"]) == ("SHORT", "10000.00", summary.EXCEPTION)
    assert summary.settlement(None, Decimal(0))["conclusion"] == summary.CANNOT
    rows = [{"label": "A", "conclusion": summary.OK},
            {"label": "B", "conclusion": summary.EXCEPTION, "impact": summary.CUSTOMER, "difference": "500"},
            {"label": "C", "conclusion": summary.CANNOT, "reason": "no price master"}]
    out = summary.verdict(rows, delivery_started=True, missing=[])
    assert out["status"] == "EXCEPTIONS" and out["amountAtRisk"]["customerOvercharged"] == "500.00"
    assert summary.verdict(rows[:1] + rows[2:], delivery_started=True, missing=[])["status"] == "INCOMPLETE"
    assert summary.verdict(rows[:1], delivery_started=True, missing=[])["status"] == "CLEAN"
    assert summary.verdict(rows[:1], delivery_started=False, missing=[])["status"] == "INCOMPLETE"


# ── the Journey (the Journey 360 section) ────────────────────────────────────
@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2asum")
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
        text("INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, component_key, "
             "source_document_type, amount, source_document_id) VALUES (:t, :j, :k, :c, :d, :a, :doc)"),
        {"t": journey.tenant_id, "j": journey.journey_id, "k": kind, "c": key, "d": document_type,
         "a": amount, "doc": uuid4()},
    )


def test_the_section_is_built_on_demand_from_the_other_tabs(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        _source(connection, journey, "COMMERCIAL", "ex_showroom_price", "tax_invoice_tally", "1005000")
        _source(connection, journey, "COMMERCIAL", "insurance_amount", "tax_invoice_tally", "40000")
        _source(connection, journey, "COMMERCIAL", "insurance_amount", "insurance_cover", "40000")
        _source(connection, journey, "DISCOUNT", "CASH_DISCOUNT", "tax_invoice_tally", "20000")
        connection.execute(
            text("INSERT INTO auditcore.discount_applications (tenant_id, journey_id, discount_key, "
                 "standard_eligible_amount, actual_discount_amount, eligibility_result, actual_source_kind) "
                 "VALUES (:t, :j, 'CASH_DISCOUNT', 25000, 20000, 'ELIGIBLE', 'EVIDENCE')"),
            {"t": journey.tenant_id, "j": journey.journey_id})
    add_ready_document(journey, "insurance_cover", process_area="DELIVERY", premium_amount="40000")
    add_evidence(journey, di_document_id=uuid4(), document_type_key="gate_pass", process_area="DELIVERY")
    add_receipt_payment(journey, amount="50000", receipt_number="R1", receipt_date="2026-09-01")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/360/audit-summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tolerance"] == "10" and set(body["rules"]) >= {"AS-01", "AS-09"}
    charges = {r["key"]: r for r in body["deal"]["charges"]}
    assert charges["ex_showroom_price"]["billed"] == "1005000.00"
    products = {r["key"]: r for r in body["products"]}
    assert products["insurance_amount"]["conclusion"] == summary.OK  # the policy shows the same premium
    cash = next(b for b in body["benefits"] if b["key"] == "CASH_DISCOUNT")
    assert (cash["conclusion"], cash["impact"], cash["difference"]) == (summary.EXCEPTION, summary.CUSTOMER, "-5000.00")
    funding = body["funding"]
    assert funding["receipts"] == "50000.00" and funding["bankCheck"]["noStatement"] == "50000.00"
    assert funding["bankCheck"]["conclusion"] == summary.CANNOT
    assert body["settlement"]["received"] == "50000.00"
    assert body["verdict"]["status"] == "EXCEPTIONS"
    assert body["verdict"]["amountAtRisk"]["customerOvercharged"] == "5000.00"


def test_road_tax_and_registration_are_one_line_per_document() -> None:
    # ADI-PB-261022-001: the challan splits it (130,492 + 1,640), the cost
    # sheet prints the whole 1,32,132 on its registration line.
    road = {"key": "road_tax_amount", "partOf": "road_tax_registration",
            "sources": [_src("rto_challan", "130492", "RTO challan")]}
    reg = {"key": "registration_charges", "partOf": "road_tax_registration",
           "sources": [_src("rto_challan", "1640", "RTO challan"), _src("cost_sheet", "132132", "Cost sheet")]}
    folded = {"key": "road_tax_registration", "label": "Road tax + registration", "standard": "174730",
              "billed": "132132", "sources": [*road["sources"], *reg["sources"]]}
    row = summary.registration_row(folded, [road, reg])
    assert {s["documentType"]: s["amount"] for s in row["sources"]} == {"rto_challan": "132132", "cost_sheet": "132132"}
    out = summary.conclude_charge(row, on_file={"rto_challan"})
    assert (out["conclusion"], out["rule"], out["proof"]["amount"]) == (summary.OK, "AS-02", "132132.00")
