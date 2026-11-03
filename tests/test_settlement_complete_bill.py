"""The settlement judged on the bill as it stands (2026-11-03).

Product owner rule (2 Nov 2026): only the billing documents count, never the
booking docket. The full live snapshot after that rule showed four ways the
balance still went wrong:

- the vehicle's own price awaiting its bill: an RTO challan's Rs 10,000 stood
  as the payable of an Rs 18.6 lakh deal, so the receipts and the loan read as
  an excess of Rs 18.5 lakh;
- an excess while lines still await their bills (the extended warranty, the
  RSA): it may be paying for them;
- a figure an earlier reading of the NDC recorded and today's reading no
  longer gives (Rs 21,75,000 "corporate discount"), never deleted from the
  source rows;
- the dealer posting three receipts as one ledger transfer, and the loan's
  own receipt (the sanction less the lender's charges) beside the letter's
  sanction: money counted twice.
"""
from __future__ import annotations

from decimal import Decimal as D

import pytest
from conftest import delete_tenant_data
from p2_support import add_ready_document, add_receipt_payment, create_p2_journey, database_engine
from sqlalchemy import text
from test_uc03_account_statement import UTKAL, _ledger, _set_policy, _sync

from audit_core.db import set_tenant_context
from audit_core.uc03_deal_source_policy import POLICY_ACCOUNT_STATEMENT
from audit_core.uc03_ledger_receipts import _match
from audit_core.uc03_p2_audit_summary import settlement


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="scb")
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


# ── the settlement ───────────────────────────────────────────────────────────
def test_an_excess_while_bills_are_awaited_is_not_judged_and_a_shortfall_stands() -> None:
    awaited = ["Extended warranty", "Road-side assistance"]
    excess = settlement(D(1041534), D(1057677), awaiting=awaited)
    assert excess["conclusion"] == "CANNOT_DETERMINE" and excess["status"] is None
    assert D(excess["balance"]) == D(-16143) and "Extended warranty, Road-side assistance" in excess["reason"]
    short = settlement(D(1097242), D(1057677), awaiting=awaited)
    assert short["status"] == "SHORT" and short["conclusion"] == "EXCEPTION"
    assert settlement(D(1041534), D(1057677))["status"] == "EXCESS"
    assert "vehicle's price" in settlement(None, D(5000))["reason"]


def test_no_payable_while_the_vehicles_price_awaits_its_bill(journey) -> None:
    """Live 2026-11-03: no NDC, no vehicle invoice; the RTO challan's
    Rs 10,000 stood as the payable."""
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    add_ready_document(journey, "rto_challan", process_area="DELIVERY", total_amount="10000",
                       registration_fee="10000")
    summary = _deal(journey)["summary"]
    assert summary["payable"] is None and summary["balanceDue"] is None


# ── figures today's reading no longer gives ──────────────────────────────────
def test_a_figure_an_earlier_reading_recorded_leaves_the_deal(journey) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    document_id, evidence_id = _ledger(journey, UTKAL)
    _sync(journey)
    before = _deal(journey)["summary"]
    # The first reading's misread benefit, left behind in the source rows.
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text(
            "INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, component_key, "
            "source_document_type, amount, source_evidence_id, source_document_id) "
            "VALUES (:t, :j, 'DISCOUNT', 'corporate_privilege', 'customer_ledger', 2175000, :e, :d)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "e": evidence_id, "d": document_id})
    view = _deal(journey)
    corporate = [r for r in view["discounts"] if r["key"] == "CORPORATE_PRIVILEGE"]
    assert all(r["current"] is None and r["billed"] is None for r in corporate)
    assert view["summary"]["discounts"]["current"] == before["discounts"]["current"]
    assert view["summary"]["payable"] == before["payable"]
    # What today's reading gives still stands.
    given = next(r for r in view["discounts"] if r["key"] == "ADDITIONAL_DISCOUNT")
    assert D(given["current"]) == D(5000)


# ── money counted once ───────────────────────────────────────────────────────
def test_one_ledger_transfer_for_several_receipts_on_file_is_those_receipts() -> None:
    """Live 2026-11-03: receipts of Rs 5,000, 11,000 and 70,000 posted as one
    Rs 86,000 journal transfer."""
    from datetime import date

    posting = {"amount": D(86000), "date": date(2026, 9, 30), "number": "25287",
               "particulars": "TRANSFER FROM ANOTHER LEDGER", "voucherType": "JOURNAL_NONGST", "mode": "OTHERS"}
    receipts = [{"receipt_number": n, "receipt_date": d, "amount": a} for n, d, a in (
        ("AMC-B/26509/26-27", date(2026, 9, 21), D(5000)), ("AMC-B/27112/26-27", date(2026, 9, 25), D(11000)),
        ("AMC-B/27110/26-27", date(2026, 9, 26), D(70000)))]
    assert _match([posting], receipts) == []
    other = {**posting, "amount": D(30000), "number": "25288"}
    assert _match([posting, other], receipts) == [other]


def test_the_loans_own_receipt_net_of_charges_is_not_counted_again_from_the_letter(journey) -> None:
    """Live 2026-11-03: a Rs 7,01,366 "DISB:" receipt against a sanction of
    Rs 7,10,000; the letter's amount was added on top of it."""
    from audit_core.uc03_p2_journey360 import _loan_counted, _paid, payments

    add_ready_document(journey, "bank_approval_letter", process_area="DELIVERY",
                       finance_sanctioned_amount="710000", financier_name="AXIS BANK LTD",
                       finance_sanction_date="2026-10-10")
    add_receipt_payment(journey, amount="701366", receipt_number="AMC-B/29960/26-27", receipt_date="2026-10-15")
    add_receipt_payment(journey, amount="21000", receipt_number="AMC-B/29411/26-27", receipt_date="2026-10-12")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("INSERT INTO auditcore.finance_records (tenant_id, journey_id, finance_type_code, "
                       "provider_name, source_kind, details) VALUES (:t, :j, 'LOAN', 'AXIS BANK LTD', 'EVIDENCE', "
                       "'{}'::jsonb)"), {"t": journey.tenant_id, "j": journey.journey_id})
        loan = _loan_counted(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                             paid=_paid(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id))
        view = payments(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert D(loan["amount"]) == D(0) and not loan["unknown"] and not loan["withoutReceipt"]
    assert "among the receipts" in loan["note"]
    assert view["loanWithoutReceipt"] is None
    assert D(view["receiptsTotal"]) == D(722366)
