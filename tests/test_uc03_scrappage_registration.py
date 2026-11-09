"""Registration on a scrappage deal (product owner 2026-11-09): a registration
shortfall that is exactly the 25% scrappage concession (within Rs 100) is not
an undercharge, and the PC is told; on a real Postgres."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import add_ready_document, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_audit_rules import run_p2_audit_rules
from audit_core.uc03_scrappage_registration import assess_line

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


def test_on_a_scrappage_deal_the_concession_is_not_an_undercharge_and_the_pc_is_told(journey):
    _deal(journey, registration_charged="60000")  # 25% of 80,000 = 20,000
    add_ready_document(journey, "scrappage_certificate_of_deposit")

    under = _run(journey)["DEAL_UNDERCHARGED"]
    assert under.outcome == "PASS", under.reason
    assert "25% scrappage concession" in under.reason
    [explained] = under.details["scrappageConcession"]
    assert explained["concession"] == "20000.00"
    task = _pc_task(journey)
    assert task["assigned_role_code"] == "PC" and task["task_status"] == "READY"
    assert "Certificate of Deposit is on file" in task["description"]


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
