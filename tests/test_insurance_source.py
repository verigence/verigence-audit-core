"""In-house or external insurance (product owner 2026-10-27): in-house when the
policy carries the dealer's Agent / MISP code or a dealer invoice bills the
premium; otherwise a PC task asks, with the code in the comment, and an in-house
code is remembered for the dealer."""
from __future__ import annotations

from uuid import uuid4

import pytest
from p2_support import (
    add_evidence,
    add_extracted_field,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_insurance_source as rule
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_tasks import submit_action


def test_codes_are_read_from_the_comment() -> None:
    assert rule.codes_in("MISP code MIBL/M&M/AAECA5786A/000 per policy") == ["MIBL/M&M/AAECA5786A/000"]
    assert rule.codes_in("Agent code 91063100000001") == ["91063100000001"]
    assert rule.codes_in("done by customer") == []
    assert rule.normalize_code("MIBL/M&M/AAECA5786A/000") == "MIBLMMAAECA5786A000"


@pytest.fixture
def engine():
    engine = database_engine()
    yield engine
    engine.dispose()


def _policy(journey, *, agent_code=None, misp_code=None, agent="Policybazaar Insurance Brokers Private Limited"):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.insurance_records (tenant_id, journey_id, insurer_name, policy_reference, "
                 "agent_intermediary_name, agent_intermediary_code, misp_code, source_kind) "
                 "VALUES (:t, :j, 'National Insurance Company Limited', :p, :a, :ac, :mc, 'EVIDENCE')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "p": uuid4().hex[:12], "a": agent,
             "ac": agent_code, "mc": misp_code})


def _sync(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        rule.sync_insurance_source_task(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        return rule.resolve(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _task(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT task_id, task_status, reference FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                 "AND task_type='INSURANCE_SOURCE_CONFIRM'"),
            {"t": journey.tenant_id, "j": journey.journey_id}).mappings().one_or_none()


def _answer(journey, task_id, answer, comment):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return submit_action(connection, tenant_id=journey.tenant_id, task_id=task_id, action="COMPLETE_ACTION",
                             actor_id=journey.actor_id, actor_role_code="PC", comment=comment,
                             details={"answer": answer})


def test_no_dealer_code_and_no_premium_asks_the_pc_and_remembers_an_inhouse_code(engine) -> None:
    first = create_p2_journey(engine, prefix="inssrc")
    _policy(first, misp_code="MIBL/M&M/AAECA5786A/000", agent="ADITYA MOTORS")
    decision = _sync(first)
    assert (decision["source"], decision["decidedBy"]) == ("INHOUSE", "UNCONFIRMED")  # premium kept meanwhile
    task = _task(first)
    assert task["task_status"] == "READY" and [a["value"] for a in task["reference"]["answers"]] == ["INHOUSE", "EXTERNAL"]

    with pytest.raises(ValueError, match="Agent code / MISP code"):
        _answer(first, task["task_id"], "INHOUSE", "yes it is ours")
    _answer(first, task["task_id"], "INHOUSE", "MISP code MIBL/M&M/AAECA5786A/000")
    assert _task(first)["task_status"] == "VERIFIED_COMPLETE"
    assert _sync(first)["decidedBy"] == "PC"

    # The code is now the dealer's: without the PC's answer, the documents decide.
    with engine.begin() as connection:
        set_tenant_context(connection, first.tenant_id)
        connection.execute(text("UPDATE auditcore.insurance_records SET insurance_source = NULL "
                                "WHERE tenant_id=:t AND journey_id=:j"), {"t": first.tenant_id, "j": first.journey_id})
    decision = _sync(first)
    assert decision["decidedBy"] == "DOCUMENT" and "MISP code" in decision["basis"]


def test_external_answer_takes_the_premium_out(engine) -> None:
    journey = create_p2_journey(engine, prefix="inssrc")
    _policy(journey, agent_code="91063100000001")  # Policybazaar
    _sync(journey)
    _answer(journey, _task(journey)["task_id"], "EXTERNAL", "Agent code 91063100000001 Policybazaar")
    decision = _sync(journey)
    assert (decision["source"], decision["decidedBy"]) == ("SELF", "PC")
    with engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert connection.execute(text("SELECT count(*) FROM auditcore.dealer_insurance_codes WHERE tenant_id=:t"),
                                  {"t": journey.tenant_id}).scalar_one() == 0  # an external code is not the dealer's


def test_a_premium_on_the_dealer_invoice_is_inhouse(engine) -> None:
    journey = create_p2_journey(engine, prefix="inssrc")
    _policy(journey, agent_code="91063100000001")
    invoice = uuid4()
    add_evidence(journey, di_document_id=invoice, document_type_key="customer_invoice_dms", process_area="DELIVERY")
    add_extracted_field(journey, di_document_id=invoice, field_key="line_items", document_type="customer_invoice_dms",
                        value='[{"description_raw": "Insurance", "net_amount": 37476.0, "line_category": "INSURANCE"}]')
    decision = _sync(journey)
    assert decision["decidedBy"] == "DOCUMENT" and "37,476" in decision["basis"]
    assert _task(journey) is None


def test_a_zero_insurance_line_or_no_policy_does_not_decide(engine) -> None:
    journey = create_p2_journey(engine, prefix="inssrc")
    assert _sync(journey)["decidedBy"] == "DEFAULT" and _task(journey) is None  # no policy yet: nothing to ask
    invoice = uuid4()
    add_evidence(journey, di_document_id=invoice, document_type_key="customer_invoice_dms", process_area="DELIVERY")
    add_extracted_field(journey, di_document_id=invoice, field_key="line_items", document_type="customer_invoice_dms",
                        value='[{"description_raw": "Insurance", "net_amount": 0.0, "line_category": "INSURANCE"}]')
    _policy(journey)
    assert _sync(journey)["decidedBy"] == "UNCONFIRMED" and _task(journey) is not None


def test_review_job_is_a_dry_run_unless_committed(engine) -> None:
    from audit_core.uc03_insurance_source_review import run_insurance_source_review
    from audit_core.uc03_p2_worker import _one_shot_target

    journey = create_p2_journey(engine, prefix="inssrc")
    _policy(journey)
    run_insurance_source_review(engine, "t-dry", [journey.tenant_id])
    assert _task(journey) is None
    run_insurance_source_review(engine, "t-commit", [journey.tenant_id])
    assert _task(journey) is not None
    target, _args = _one_shot_target("INSURANCE_SOURCE_REVIEW", engine=engine, tag="x", reports=[])
    assert target is run_insurance_source_review
