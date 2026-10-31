"""Discount variance, tallied from the invoice only (product owner, 2026-10-24)."""
from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_discount_variance import (
    OVER,
    UNDER,
    UNKNOWN,
    discount_variance_report,
    discount_variance_rows,
)


def _view(discounts, *, invoice=True, price="1000000", policy="INVOICE", standard="1000000"):
    return {
        "summary": {"vehicleInvoiceOnFile": invoice, "billedSource": policy},
        "categories": [{"code": "VEHICLE", "components": [
            {"key": "ex_showroom_price", "standard": standard, "current": price}]}],
        "discounts": discounts,
    }


def _d(key, entitled, billed, billed_from="invoice", booking=None, effective=None, eligibility=None, sources=None):
    return {"key": key, "label": key.title(), "entitled": entitled, "billed": billed, "billedFrom": billed_from,
            "booking": booking, "effective": effective, "eligibility": eligibility, "sources": sources or []}


def test_a_discount_given_above_its_entitlement_is_over():
    rows = discount_variance_rows(_view([_d("CORPORATE", "20000", "35000.00", sources=[
        {"document": "Tax invoice", "documentType": "tax_invoice_tally", "amount": "35000.00"}])]))
    assert rows == [{"key": "CORPORATE", "label": "Corporate", "status": OVER, "entitled": "20000",
                     "invoice": "35000.00", "variance": "15000.00", "givenFrom": "billed",
                     "givenDocument": "Tax invoice"}]


def test_a_discount_nothing_gives_a_figure_on_is_no_discount():
    # as the EXCESS_DISCOUNT rule reads it (2026-10-31)
    assert discount_variance_rows(_view([_d("CORPORATE", "20000", None, billed_from=None)])) == []


def test_a_discount_given_below_its_entitlement_is_under():
    rows = discount_variance_rows(_view([_d("CASH_DISCOUNT", "25000", "10000")]))
    assert rows[0]["status"] == UNDER and rows[0]["variance"] == "-15000"


def test_within_rupees_100_is_not_a_variance():
    assert discount_variance_rows(_view([_d("CORPORATE", "20000", "20100")])) == []
    assert discount_variance_rows(_view([_d("CORPORATE", "20000", "19900")])) == []
    assert discount_variance_rows(_view([_d("CORPORATE", "20000", "20100.01")]))[0]["status"] == OVER


def test_the_given_figure_is_the_rules_billed_then_booking_then_reconciled():
    # 2026-10-31: the tab and the EXCESS_DISCOUNT rule read one resolution.
    statement = [{"document": "NDC (Account Statement)", "documentType": "account_statement", "amount": "30000"},
                 {"document": "Booking docket", "documentType": "booking_docket", "amount": "20000"}]
    row = discount_variance_rows(_view([_d("CASH_DISCOUNT", "25000", "30000", billed_from="document",
                                           booking="20000", sources=statement)]))[0]
    assert (row["status"], row["invoice"], row["givenFrom"]) == (OVER, "30000", "billed")
    assert row["givenDocument"] == "NDC (Account Statement)"
    row = discount_variance_rows(_view([_d("CASH_DISCOUNT", "0", None, billed_from=None, booking="20000",
                                           sources=statement[1:])]))[0]
    assert (row["status"], row["givenFrom"], row["givenDocument"]) == (OVER, "booking", "Booking docket")
    row = discount_variance_rows(_view([_d("CASH_DISCOUNT", "0", None, billed_from=None, effective="5000")]))[0]
    assert (row["givenFrom"], row["givenDocument"]) == ("effective", "Discount reconciliation")


def test_no_entitlement_on_record_is_an_entitlement_of_zero():
    # The rule's "given with no entitlement on record" (2026-10-31): the tab
    # had these as "entitlement unknown" and the screen dropped them.
    rows = discount_variance_rows(_view([_d("CORPORATE", None, "30000", billed_from="document")]))
    assert (rows[0]["status"], rows[0]["entitled"], rows[0]["variance"]) == (OVER, "0", "30000")


def test_an_unknown_entitlement_is_reported_not_taken_as_zero():
    rows = discount_variance_rows(_view([_d("EXCHANGE", None, "10000", eligibility="MASTER_UNMATCHED")]))
    assert rows[0]["status"] == UNKNOWN and rows[0]["variance"] is None
    assert discount_variance_rows(_view([_d("EXCHANGE", None, None, billed_from=None,
                                            eligibility="MASTER_UNMATCHED")])) == []


def test_an_invoice_project_is_tallied_only_once_the_vehicle_invoice_is_read():
    assert discount_variance_rows(_view([_d("CORPORATE", "20000", "40000")], invoice=False)) == []
    assert discount_variance_rows(_view([_d("CORPORATE", "20000", "40000")], price=None)) == []


def test_an_account_statement_project_needs_no_vehicle_invoice():
    # Mahindra Orissa (2026-10-31): no invoice is read into its Deal; the NDC,
    # else the cost sheet standing in for it, is what was given.
    cost_sheet = [{"document": "Cost sheet (no NDC on file)", "documentType": "cost_sheet_in_place_of_ndc",
                   "amount": "45000"}]
    rows = discount_variance_rows(_view([_d("CORPORATE", "20000", "45000", billed_from="document",
                                            sources=cost_sheet)],
                                        invoice=False, price=None, policy="ACCOUNT_STATEMENT"))
    assert rows[0]["status"] == OVER and rows[0]["givenDocument"] == "Cost sheet (no NDC on file)"


def test_no_standard_on_record_is_not_tallied_as_the_rule_skips():
    assert discount_variance_rows(_view([_d("CORPORATE", "20000", "40000")], standard=None,
                                        policy="ACCOUNT_STATEMENT")) == []


def test_the_tab_lists_exactly_the_discounts_the_rule_flags():
    from audit_core.uc03_p2_audit_rules import excess_discount

    lines = [
        _d("CASH_DISCOUNT", "25000", "30000", billed_from="document"),
        _d("CORPORATE", None, None, billed_from=None, booking="13000"),
        _d("EXCHANGE_BONUS", "20000", "20050", billed_from="document"),
        _d("SCRAPPAGE", None, "20000", billed_from="document", eligibility="MASTER_UNMATCHED"),
        _d("MANAGEMENT_REFERRAL", "0", None, billed_from=None),
    ]
    view = _view(lines, invoice=False, price=None, policy="ACCOUNT_STATEMENT")

    class _Facts:
        sheet = view

    outcome = excess_discount(_Facts())
    assert outcome.outcome == "FAIL"
    flagged = {d["key"]: d["extra"] for d in outcome.details["discounts"]}
    over = {r["key"]: r["variance"] for r in discount_variance_rows(view) if r["status"] == OVER}
    assert flagged == over == {"CASH_DISCOUNT": "5000", "CORPORATE": "13000"}


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="discv")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def test_the_report_carries_category_pc_outlet_customer_and_model(journey):
    rows = [{"key": "CORPORATE", "label": "Corporate", "status": OVER, "entitled": "20000",
             "invoice": "35000", "variance": "15000"}]
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_journey_runtime (tenant_id, journey_id, current_stage, discount_variance) "
                 "VALUES (:t, :j, 'BOOKING_VERIFY_DOCUMENTS', CAST(:d AS jsonb)) "
                 "ON CONFLICT (tenant_id, journey_id) DO UPDATE SET discount_variance = EXCLUDED.discount_variance"),
            {"d": json.dumps(rows), "t": journey.tenant_id, "j": journey.journey_id},
        )
        report = discount_variance_report(connection, tenant_id=journey.tenant_id)
    assert report["counts"]["over"] == 1 and report["counts"]["journeys"] == 1
    item = report["items"][0]
    assert {"discount", "pc", "outlet", "customer", "model", "variant", "variance"} <= set(item)
    assert item["discountKey"] == "CORPORATE" and Decimal(item["variance"]) == 15000
    assert UNDER and report["toleranceRupees"] == 100


def _sql(journey, sql, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _statement_deal_with_excess(journey):
    """A Mahindra Orissa deal: the NDC gives Rs 30,000 cash discount against an
    entitlement of Rs 25,000, and Rs 13,000 corporate with none on record. The
    invoice (withheld from this Project's Deal) says otherwise."""
    _sql(journey, "UPDATE auditcore.projects SET deal_source_policy='ACCOUNT_STATEMENT' WHERE tenant_id=:t")
    _sql(journey, "INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount, "
                  "actual_amount) VALUES (:t, :j, 'ex_showroom_price', 1000000, 1000000)")
    for kind, key, document_type, amount in (
        ("COMMERCIAL", "ex_showroom_price", "customer_ledger", "1000000"),
        ("DISCOUNT", "CASH_DISCOUNT", "customer_ledger", "30000"),
        ("DISCOUNT", "CORPORATE_DISCOUNT", "customer_ledger", "13000"),
        ("DISCOUNT", "CASH_DISCOUNT", "tax_invoice_tally", "25000"),
    ):
        _sql(journey, "INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, "
                      "component_key, source_document_type, amount, source_document_id) "
                      "VALUES (:t, :j, :k, :c, :d, :a, :doc)", k=kind, c=key, d=document_type, a=amount, doc=uuid4())
    _sql(journey, "INSERT INTO auditcore.discount_applications (tenant_id, journey_id, discount_key, "
                  "standard_eligible_amount, actual_discount_amount, eligibility_result, actual_source_kind) "
                  "VALUES (:t, :j, 'CASH_DISCOUNT', 25000, 30000, 'ELIGIBLE', 'EVIDENCE')")
    _sql(journey, "INSERT INTO auditcore.p2_journey_runtime (tenant_id, journey_id, current_stage) "
                  "VALUES (:t, :j, 'BOOKING_VERIFY_DOCUMENTS') ON CONFLICT (tenant_id, journey_id) DO NOTHING")


def test_an_account_statement_deal_shows_every_discount_the_rule_flags(journey):
    from audit_core.uc03_p2_audit_rules import run_p2_audit_rules
    from audit_core.uc03_p2_journey360 import deal
    from audit_core.uc03_p2_price_variance import refresh_price_variance

    _statement_deal_with_excess(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        view = deal(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        assert view["summary"]["billedSource"] == "ACCOUNT_STATEMENT"
        outcome = {r.code: r for r in run_p2_audit_rules(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, stage="BOOKING",
            correlation_id="test", triggering_event="TEST")}["EXCESS_DISCOUNT"]
        refresh_price_variance(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        report = discount_variance_report(connection, tenant_id=journey.tenant_id)
    assert outcome.outcome == "FAIL"
    flagged = {d["key"]: Decimal(d["extra"]) for d in outcome.details["discounts"]}
    shown = {i["discountKey"]: Decimal(i["variance"]) for i in report["items"] if i["status"] == OVER}
    assert flagged == shown and shown["CASH_DISCOUNT"] == 5000
    cash = next(i for i in report["items"] if i["discountKey"] == "CASH_DISCOUNT")
    assert cash["invoice"] == "30000.00" and cash["givenDocument"] == "NDC (Account Statement)"


def test_a_cancelled_booking_is_not_reported_and_over_grants_come_first(journey):
    rows = [{"key": f"U{i}", "label": "Under", "status": UNDER, "entitled": "90000", "invoice": "0",
             "variance": "-90000"} for i in range(3)]
    rows.append({"key": "CORPORATE", "label": "Corporate", "status": OVER, "entitled": "0",
                 "invoice": "500", "variance": "500"})
    _sql(journey, "INSERT INTO auditcore.p2_journey_runtime (tenant_id, journey_id, current_stage, discount_variance) "
                  "VALUES (:t, :j, 'BOOKING_VERIFY_DOCUMENTS', CAST(:d AS jsonb)) ON CONFLICT (tenant_id, journey_id) "
                  "DO UPDATE SET discount_variance = EXCLUDED.discount_variance", d=json.dumps(rows))
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        first = discount_variance_report(connection, tenant_id=journey.tenant_id, limit=1)["items"]
    assert [i["status"] for i in first] == [OVER]

    _sql(journey, "INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, business_status) "
                  "VALUES (:t, :j, 'BOOKING', 'BOOKING_CANCELLED') ON CONFLICT (tenant_id, journey_id, stage_code) "
                  "DO UPDATE SET business_status = 'BOOKING_CANCELLED'")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert discount_variance_report(connection, tenant_id=journey.tenant_id)["items"] == []


def test_the_recompute_job_rebuilds_old_rows_only_on_commit(journey):
    from audit_core.uc03_discount_variance_recompute import (
        run_discount_variance_recompute,
    )

    _statement_deal_with_excess(journey)
    stale = [{"key": "CASH_DISCOUNT", "label": "Cash", "status": UNDER, "entitled": "25000", "invoice": "0",
              "variance": "-25000"}]
    _sql(journey, "UPDATE auditcore.p2_journey_runtime SET discount_variance=CAST(:d AS jsonb), "
                  "price_variance_fact_version=fact_version WHERE tenant_id=:t AND journey_id=:j", d=json.dumps(stale))

    def cached():
        return _sql(journey, "SELECT discount_variance FROM auditcore.p2_journey_runtime "
                             "WHERE tenant_id=:t AND journey_id=:j").scalar_one()

    run_discount_variance_recompute(journey.engine, "xd-1", [journey.tenant_id])
    assert cached() == stale
    run_discount_variance_recompute(journey.engine, "xd-1-commit", [journey.tenant_id])
    assert {r["key"]: r["status"] for r in cached()} == {"CASH_DISCOUNT": OVER, "CORPORATE_DISCOUNT": OVER}
