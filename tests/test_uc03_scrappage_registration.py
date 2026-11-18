"""Registration on a scrappage deal (product owner 2026-11-09): a registration
shortfall that is exactly the 25% scrappage concession (within Rs 100) is not
an undercharge, and the journey's System remarks say so (2026-11-15: no
longer a PC task); on a real Postgres."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import add_ready_document, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_audit_rules import run_p2_audit_rules
from audit_core.uc03_scrappage_registration import (
    apply_concession,
    assess_line,
    challan_rebate,
    deal_block,
)
from audit_core.uc03_system_remarks import system_remarks

# ── the line on its own ──────────────────────────────────────────────────────

def test_a_live_scrappage_shortfall_is_the_concession():
    # 2026-11-09, live: Rs 1,43,367 on the NDC against Rs 1,91,156.15.
    line = assess_line(standard=Decimal("191156.15"), charged=Decimal(143367), scrappage="a CoD is on file")
    assert line["explained"] and line["concession"] == Decimal("47789.04")


def test_the_concession_holds_within_rs_100_only():
    standard = Decimal("181940.10")  # 25% = 45,485.03
    assert assess_line(standard=standard, charged=standard - Decimal("45525.10"), scrappage="x")["explained"]
    off = assess_line(standard=standard, charged=standard - Decimal(45700), scrappage="x")
    assert not off["explained"] and "would be" in off["note"]


def test_a_25_percent_shortfall_without_scrappage_on_file_stays_short():
    line = assess_line(standard=Decimal(80000), charged=Decimal(60000), scrappage=None)
    assert not line["explained"] and "no Certificate of Deposit" in line["note"]


# ── the rule ─────────────────────────────────────────────────────────────────

@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine)
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _run(journey) -> dict:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return {r.code: r for r in run_p2_audit_rules(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, stage="BOOKING",
            correlation_id="test", triggering_event="TEST")}


def _line(journey, key: str, standard: str, actual: str) -> None:
    _sql(journey, "INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount, "
                  "actual_amount) VALUES (:t, :j, :k, :s, :a)", k=key, s=standard, a=actual)
    _sql(journey, "INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, "
                  "component_key, source_document_type, amount, source_document_id) "
                  "VALUES (:t, :j, 'COMMERCIAL', :k, 'tax_invoice_tally', :a, :doc)", k=key, a=actual, doc=uuid4())


def _pc_task(journey):
    return _sql(journey, "SELECT task_status, assigned_role_code, description FROM auditcore.p2_tasks "
                         "WHERE tenant_id=:t AND dedupe_key=:k",
                k=f"scrappage-registration:{journey.journey_id}").mappings().one_or_none()


def _deal(journey, *, registration_charged: str) -> None:
    _line(journey, "ex_showroom_price", "1000000", "1000000")
    _line(journey, "registration_charges", "80000", registration_charged)


def test_on_a_scrappage_deal_the_concession_is_not_an_undercharge_and_is_a_system_remark(journey):
    _deal(journey, registration_charged="60000")  # 25% of 80,000 = 20,000
    add_ready_document(journey, "scrappage_certificate_of_deposit")

    under = _run(journey)["DEAL_UNDERCHARGED"]
    assert under.outcome == "PASS", under.reason
    assert "scrappage concession of ₹20,000 (25% of the RTO standard)" in under.reason
    [explained] = under.details["scrappageConcession"]
    assert explained["concession"] == "20000.00"
    # Product owner 2026-11-15: a remark on the journey, not a PC task.
    assert _pc_task(journey) is None
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        remarks = system_remarks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    [remark] = [r for r in remarks["remarks"] if r["code"] == "SCRAPPAGE_CONCESSION"]
    assert "scrappage concession of ₹20,000" in remark["text"] and "not counted as an undercharge" in remark["text"]


def test_an_information_task_raised_before_is_closed_saying_where_the_note_went(journey):
    _deal(journey, registration_charged="60000")
    add_ready_document(journey, "scrappage_certificate_of_deposit")
    _sql(journey, "INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, "
                  "source_type, source_code, dedupe_key, title, description, reference, severity, priority, "
                  "assigned_role_code, allowed_actions, completion_protocol) VALUES (:t, :j, "
                  "'SCRAPPAGE_REGISTRATION_CONCESSION', 'CROSS_DOCUMENT_REVIEW', 'SYSTEM', 'RESOLVER', "
                  "'SCRAPPAGE_REGISTRATION_CONCESSION', :k, 'Registration lower', 'x', '{}', 'MEDIUM', 'NORMAL', "
                  "'PC', '[]', 'MACHINE_VERIFIED')", k=f"scrappage-registration:{journey.journey_id}")
    _run(journey)
    assert _pc_task(journey)["task_status"] == "VERIFIED_COMPLETE"


def test_without_scrappage_on_file_a_25_percent_shortfall_stays_and_says_why(journey):
    _deal(journey, registration_charged="60000")
    under = _run(journey)["DEAL_UNDERCHARGED"]
    assert under.outcome == "FAIL"
    assert "matches the 25% scrappage concession, but no Certificate of Deposit" in under.reason
    assert _pc_task(journey) is None


def test_on_a_scrappage_deal_a_different_shortfall_stays_with_the_expected_concession(journey):
    _deal(journey, registration_charged="50000")  # 30,000 short, 10,000 more than the concession
    add_ready_document(journey, "scrappage_certificate_of_deposit")
    under = _run(journey)["DEAL_UNDERCHARGED"]
    assert under.outcome == "FAIL"
    assert "the 25% concession would be ₹20,000" in under.reason
    assert _pc_task(journey) is None


# ── the Deal owns the concession (product owner 2026-11-17) ──────────────────

_CHALLAN = {"documentId": "c1", "fields": {"line_items": (
    '[{"description_raw": "Hypothecation Addition", "amount": 1500, "rebate_waiver_amount": null, "total": 1500}, '
    '{"description_raw": "MV Tax(13-Oct-2026 to One Time)", "amount": 235899, "rebate_waiver_amount": 58975, '
    '"total": 176924}, {"description_raw": "Automation and Technology Fee", "amount": 140, "total": 140}]')}}


def test_the_challan_prints_the_rebate_the_rto_granted():
    # ADI-RD-261102-003 (PRUTHIRAJ MUDULI), live 2026-11-17
    assert challan_rebate([_CHALLAN]) == {"rebate": Decimal(58975), "tax": Decimal(235899), "documentId": "c1"}
    assert challan_rebate([{"fields": {"line_items": '[{"description_raw": "MV Tax", "amount": 100}]'}}]) is None


def _rto(standard: str, billed: str) -> list[dict]:
    return [{"key": "registration_charges", "label": "Registration", "standard": standard, "billed": billed,
             "flags": []}]


def test_the_deal_takes_the_challans_rebate_off_the_rto_standard():
    # Live: standard 2,36,639.97 + 1,500 hypothecation; the NDC charges 1,78,564.
    rows = _rto("238139.97", "178564")
    facts = {"certificates": [{"certificateNumber": "COD2026100HR20AK5127"}], "challan": challan_rebate([_CHALLAN])}
    applied = apply_concession(rows, facts, benefits=[])
    assert applied["concession"] == "58975" and applied["source"].startswith("the scrappage rebate")
    assert rows[0]["standard"] == "179164.97" and rows[0]["standardMaster"] == "238139.97"
    assert "SCRAPPAGE_CONCESSION" in rows[0]["flags"] and "SCRAPPAGE_COD_MISSING" not in rows[0]["flags"]
    block = deal_block(facts, applied=applied, benefits=[])
    assert block["challanRebate"] == "58975" and not block["codMissing"]


def test_a_rebate_with_no_certificate_on_file_is_taken_and_the_certificate_is_missing():
    rows = _rto("238139.97", "178564")
    facts = {"certificates": [], "challan": challan_rebate([_CHALLAN])}
    applied = apply_concession(rows, facts, benefits=[])
    assert applied and "SCRAPPAGE_COD_MISSING" in rows[0]["flags"]
    assert deal_block(facts, applied=applied, benefits=[])["codMissing"]


def test_no_rebate_and_no_scrappage_leaves_the_standard_alone():
    rows = _rto("80000", "60000")
    assert apply_concession(rows, {"certificates": [], "challan": None}, benefits=[]) is None
    assert rows[0]["standard"] == "80000"
    assert deal_block({"certificates": [], "challan": None}, applied=None, benefits=[]) is None


def test_a_scrappage_benefit_given_lets_the_25_percent_shortfall_be_the_concession():
    rows = _rto("80000", "60000")
    applied = apply_concession(rows, {"certificates": [], "challan": None},
                               benefits=[{"key": "SCRAPPAGE_BONUS_DEALER", "label": "Scrappage bonus (dealer)",
                                          "current": "20000"}])
    assert applied["concession"] == "20000.00" and rows[0]["standard"] == "60000.00"
