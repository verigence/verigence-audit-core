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


def test_a_benefit_the_deal_does_not_count_is_never_shown_as_given() -> None:
    """Live 2026-11-03 (SHI-SS-261027-024): the NDC's "Exchange / loyalty
    bonus" Rs 10,000 is the ledger's scrappage credit note. The Deal counted it
    once; this page showed both as given and asked for an exchange vehicle RC."""
    unknown = ENTITLEMENT_UNKNOWN_RESULTS
    exchange = summary.conclude_benefit(
        {"key": "EXCHANGE_BONUS", "label": "Exchange bonus", "entitled": "0", "eligibility": "ELIGIBLE",
         "billed": None, "current": None, "sameAs": {"key": "SCRAPPAGE_BONUS_DEALER", "label": "Scrappage bonus (dealer)"},
         "proof": {"documentType": "vehicle_rc", "document": "Vehicle RC", "onFile": False},
         "sources": [_src("account_statement", "10000")]}, unknown_results=unknown)
    assert (exchange["conclusion"], exchange["billed"], exchange["proof"]) == (summary.INFO, None, None)
    assert "same benefit as Scrappage bonus (dealer)" in exchange["reason"]
    in_price = summary.conclude_benefit(
        {"key": "CASH_DISCOUNT", "label": "Consumer / cash discount", "billed": "49999.60", "current": None,
         "inPrice": True, "sources": [_src("customer_invoice_dms", "49999.60")]}, unknown_results=unknown)
    assert (in_price["conclusion"], in_price["billed"]) == (summary.INFO, None)
    invoice_only = summary.conclude_benefit(
        {"key": "CASH_DISCOUNT", "label": "Consumer / cash discount", "billed": "5000", "current": None,
         "flags": ["NOT_ON_STATEMENT"], "entitled": "5000", "eligibility": "ELIGIBLE",
         "sources": [_src("customer_invoice_dms", "5000")]}, unknown_results=unknown)
    assert (invoice_only["conclusion"], invoice_only["billed"]) == (summary.DISPUTED, None)
    assert invoice_only["documents"][0]["amount"] == "5000.00"
    unflagged = summary.conclude_benefit(
        {"key": "UNCLASSIFIED_DISCOUNT", "label": "Invoice discount (type not printed)", "billed": None,
         "current": None, "flags": [], "sources": [_src("customer_invoice_dms", "9000")]}, unknown_results=unknown)
    assert (unflagged["conclusion"], unflagged["billed"]) == (summary.DISPUTED, None)


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


def test_the_loan_receipt_shows_under_loan_received_not_receipts_and_is_counted_once(journey):
    # 2026-11-02: the loan paid against a receipt was under receipts, the loan 0.
    add_evidence(journey, di_document_id=uuid4(), document_type_key="gate_pass", process_area="DELIVERY")
    add_receipt_payment(journey, amount="50000", receipt_number="R1", receipt_date="2026-09-01")
    add_receipt_payment(journey, amount="650000", receipt_number="R2", receipt_date="2026-09-12")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.finance_records (tenant_id, journey_id, finance_type_code, provider_name, "
                 "financed_amount, source_kind, loan_disbursement_payment_id, loan_disbursement_amount, "
                 "loan_disbursement_confidence) SELECT tenant_id, journey_id, 'LOAN', 'HDFC BANK LTD', 1500, 'EVIDENCE', "
                 "payment_id, amount, 'HIGH' FROM auditcore.payments WHERE tenant_id=:t AND journey_id=:j "
                 "AND receipt_number='R2'"),
            {"t": journey.tenant_id, "j": journey.journey_id})
        result = summary.audit_summary(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    funding = result["funding"]
    assert funding["receipts"] == "50000.00" and funding["loanDisbursed"] == "650000.00"
    assert funding["loanSource"] == "Loan disbursement on the finance record"
    assert funding["received"] == "700000.00" and result["settlement"]["received"] == "700000.00"
    # Every counted receipt, the loan's too, is checked against the bank statement.
    assert funding["bankCheck"]["noStatement"] == "700000.00"


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


# ── the loan (2026-10-30, REBARANI GANTHIA) ──────────────────────────────────

def test_a_financed_deal_with_no_loan_amount_cannot_be_judged_not_short() -> None:
    loan = summary.loan_received(matched=None, disbursement_known=False, financed=True, stated=None,
                                 receipt_amounts=[Decimal(325008)])
    assert loan["unknown"] is True and loan["amount"] is None
    settle = summary.settlement(Decimal(1950000), Decimal(325008), loan_unknown=True)
    assert settle["conclusion"] == summary.CANNOT and settle["status"] is None and settle["balance"] is None


def test_the_settlement_printouts_finance_do_is_the_loan_and_its_refund_is_paid_out() -> None:
    stated = {"loan": Decimal(1649800), "refunds": Decimal(24143), "source": "Settlement (ERP printout)",
              "documentId": "d"}
    loan = summary.loan_received(matched=None, disbursement_known=False, financed=True, stated=stated,
                                 receipt_amounts=[Decimal(50000), Decimal(275008)])
    assert loan["amount"] == Decimal(1649800) and loan["source"] == "Settlement (ERP printout)"
    received = Decimal(325008) + loan["amount"] - loan["refunds"]
    settle = summary.settlement(Decimal(1900758), received)
    assert settle["status"] == "EXCESS" and settle["balance"] == "-49907.00"


def test_a_statement_loan_already_among_the_receipts_is_not_counted_twice() -> None:
    stated = {"loan": Decimal(900000), "refunds": Decimal(0), "source": "NDC (account statement)", "documentId": "d"}
    loan = summary.loan_received(matched=None, disbursement_known=False, financed=True, stated=stated,
                                 receipt_amounts=[Decimal(900005)])
    assert loan["amount"] == Decimal(0) and "not counted twice" in loan["note"]
    assert "not yet linked as the loan receipt" in loan["note"]


def test_a_disbursement_on_the_finance_record_wins_over_the_statement() -> None:
    stated = {"loan": Decimal(900000), "refunds": Decimal(0), "source": "NDC (account statement)", "documentId": "d"}
    loan = summary.loan_received(matched=Decimal(850000), disbursement_known=True, financed=True, stated=stated,
                                 receipt_amounts=[])
    assert loan["amount"] == Decimal(850000) and loan["source"] == "Loan disbursement on the finance record"


def test_a_cash_deal_has_no_loan_and_is_judged() -> None:
    loan = summary.loan_received(matched=None, disbursement_known=False, financed=False, stated=None,
                                 receipt_amounts=[Decimal(100)])
    assert loan["unknown"] is False and loan["amount"] is None


# ── documents required to complete the file (2026-10-30) ─────────────────────

def test_the_required_documents_list_the_missing_first_and_make_the_verdict_incomplete(journey) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        documents = summary.required_documents(connection, tenant_id=journey.tenant_id,
                                               journey_id=journey.journey_id)
        result = summary.audit_summary(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert documents, "a new booking needs its mandatory documents"
    assert all(d["stage"] == "BOOKING" for d in documents)  # delivery not started
    on_file = [d["onFile"] for d in documents]
    assert on_file == sorted(on_file)  # missing (False) first
    assert result["requiredDocuments"] == documents
    if not all(on_file):
        assert result["verdict"]["status"] == "INCOMPLETE"
