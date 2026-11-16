"""Findings and tasks invariants (closing list item 7, 2026-11-16)."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_finding_invariants import (
    base_code,
    decide,
    enforce_journey,
    finding_may_open,
)
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_task_producer import _json

R = get_registry()


def _decide(rule_key, status=None, *, origin="MACHINE", person=None, re_on=True, cancelled=None):
    return decide(rule_key=rule_key, origin_kind=origin, control_status=status, person_decision=person,
                  rule_engine_on=re_on, registry=R, task_cancelled=cancelled)


def test_base_code():
    assert base_code("DOCUMENT_MISSING:abc") == ("DOCUMENT_MISSING", False)
    assert base_code("RE_GATE_PASS_MISSING") == ("GATE_PASS_MISSING", True)


def test_decisions():
    assert _decide("DEAL_TOTAL_VARIANCE_NEGATIVE").reason == "CHECK_RETIRED"
    assert _decide("RE_KYC_NAME_VS_BOOKING", re_on=False).reason == "RULE_ENGINE_OFF"
    assert _decide("INVOICE_DATE_BEFORE_BOOKING", "FAIL").reason == "INFORMATIONAL"
    assert _decide("TCS_SHORT", "PASS").reason == "CHECK_PASSES"
    assert _decide("TCS_SHORT", "NOT_APPLICABLE").disposition == "FIXED"
    assert _decide("WF_BOOKING_INCOMPLETE_AT_DELIVERY_START", "WAITING_FOR_FACTS").reason == "CHECK_CANNOT_DECIDE"
    # Waiting is the ask: the finding stays.
    assert _decide("BK_MIN_BOOKING_AMOUNT_NOT_MET", "WAITING_FOR_FACTS").action == "KEEP"
    assert _decide("TCS_SHORT", "FAIL").action == "KEEP"
    fp = _decide("TCS_SHORT", "FAIL", person={"outcome": "FALSE_POSITIVE", "verdict": "FALSE_POSITIVE"})
    assert (fp.reason, fp.disposition) == ("DECIDED_ON_TASK", "FALSE_POSITIVE")
    accepted = _decide("TCS_SHORT", "FAIL", person={"outcome": "EXCEPTION_ACCEPTED", "acceptedIssueHash": "x"})
    assert accepted.disposition == "FALSE_POSITIVE"
    assert _decide("TCS_SHORT", "FAIL", cancelled={"outcome": "X"}).reason == "TASK_CANCELLED"
    # A person's finding is never touched.
    assert _decide("ANYTHING", origin="HUMAN").action == "KEEP"


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2inv")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _finding(journey, code):
    return str(_sql(
        journey,
        "INSERT INTO auditcore.audit_findings (tenant_id, journey_id, finding_type_code, severity, finding_status, "
        "title, description, stage_code, origin_kind, origin_role_snapshot, rule_key, blocking_completion, "
        "finding_class) VALUES (:t, :j, 'PAYMENT_EXCEPTION', 'HIGH', 'OPEN', :title, 'x', 'BOOKING', 'MACHINE', "
        "'SYSTEM', :code, FALSE, 'VIOLATION') RETURNING audit_finding_id", title=code, code=code,
    ).scalar_one())


def _state(journey, code, status):
    _sql(journey, "INSERT INTO auditcore.p2_control_state (tenant_id, journey_id, control_code, executor_type, "
                  "control_mode, control_status, stage_code, status_reason) "
                  "VALUES (:t, :j, :c, 'NATIVE', 'RERUN', :s, 'BOOKING', 'short by Rs 500')", c=code, s=status)


def _task(journey, code, *, status="READY", finding=None, key=None, result=None):
    template = R.tasks["FINDING_REVIEW"]
    return str(_sql(
        journey,
        "INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, source_type, "
        "source_code, dedupe_key, title, description, reference, severity, priority, assigned_role_code, "
        "allowed_actions, completion_protocol, task_status, completion_result) VALUES (:t, :j, 'FINDING_REVIEW', "
        ":cat, 'SYSTEM', 'RULE', :code, :key, :code, 'x', CAST(:ref AS jsonb), 'HIGH', 'HIGH', 'TL', "
        "CAST(:actions AS jsonb), 'MACHINE_VERIFIED', :status, CAST(:result AS jsonb)) RETURNING task_id",
        cat=template.category, code=code, key=key or f"control:{journey.journey_id}:{code}",
        ref=_json({"findingId": finding} if finding else {}), actions=_json(list(template.actions)),
        status=status, result=_json(result or {}),
    ).scalar_one())


def _enforce(journey, commit=True):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return enforce_journey(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                               commit=commit, rule_engine_on=False)


def _finding_status(journey, fid):
    return _sql(journey, "SELECT finding_status, disposition FROM auditcore.audit_findings "
                         "WHERE tenant_id=:t AND audit_finding_id=:f", f=fid).one()


def _task_status(journey, tid):
    return _sql(journey, "SELECT task_status FROM auditcore.p2_tasks WHERE tenant_id=:t AND task_id=:id",
                id=tid).scalar_one()


def test_retired_and_passing_findings_close_with_their_tasks(journey):
    retired = _finding(journey, "DEAL_TOTAL_VARIANCE_NEGATIVE")
    passing = _finding(journey, "TCS_SHORT")
    _state(journey, "TCS_SHORT", "PASS")
    task = _task(journey, "TCS_SHORT", finding=passing)
    dry = _enforce(journey, commit=False)
    assert dry["finding:CHECK_RETIRED"] == 1 and dry["finding:CHECK_PASSES"] == 1
    assert _finding_status(journey, retired)[0] == "OPEN"
    _enforce(journey)
    assert _finding_status(journey, retired)[0] == "RESOLVED"
    assert tuple(_finding_status(journey, passing)) == ("RESOLVED", "FIXED")
    assert _task_status(journey, task) == "VERIFIED_COMPLETE"
    assert not _enforce(journey)  # idempotent


def test_open_finding_gets_its_one_task(journey):
    fid = _finding(journey, "DEAL_UNDERCHARGED")
    _state(journey, "DEAL_UNDERCHARGED", "FAIL")
    counts = _enforce(journey)
    assert counts["task:raised_for_open_finding"] == 1
    rows = _sql(journey, "SELECT task_status, reference->>'findingId' f FROM auditcore.p2_tasks "
                         "WHERE tenant_id=:t AND journey_id=:j AND source_code='DEAL_UNDERCHARGED'").all()
    assert [(r.task_status, r.f) for r in rows] == [("READY", fid)]
    assert not _enforce(journey)


def test_unlinked_task_is_linked_and_a_second_one_cancelled(journey):
    fid = _finding(journey, "DUPLICATE_BOOKING")
    _state(journey, "DUPLICATE_BOOKING", "FAIL")
    own = _task(journey, "DUPLICATE_BOOKING")
    extra = _task(journey, "DUPLICATE_BOOKING", finding=fid, key=f"other:{journey.journey_id}")
    counts = _enforce(journey)
    assert counts["task:linked_to_finding"] == 1 and counts["task:duplicate_cancelled"] == 1
    assert _task_status(journey, own) == "READY" and _task_status(journey, extra) == "CANCELLED"


def test_a_person_decision_closes_the_finding_and_stops_a_new_one(journey):
    fid = _finding(journey, "PAYMENT_BEFORE_BOOKING")
    _state(journey, "PAYMENT_BEFORE_BOOKING", "FAIL")
    _task(journey, "PAYMENT_BEFORE_BOOKING", status="VERIFIED_COMPLETE",
          result={"outcome": "EXCEPTION_ACCEPTED", "acceptedIssueHash": "h", "acceptedBy": "tl-1"})
    assert _enforce(journey)["finding:DECIDED_ON_TASK"] == 1
    assert tuple(_finding_status(journey, fid)) == ("RESOLVED", "FALSE_POSITIVE")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert not finding_may_open(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                    code="PAYMENT_BEFORE_BOOKING")
        assert not finding_may_open(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                    code="INVOICE_DATE_BEFORE_BOOKING")
        assert finding_may_open(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                code="TCS_SHORT")


def test_task_closes_when_its_finding_closed(journey):
    fid = _finding(journey, "TCS_SHORT")
    _sql(journey, "UPDATE auditcore.audit_findings SET finding_status='RESOLVED' WHERE tenant_id=:t "
                  "AND audit_finding_id=:f", f=fid)
    task = _task(journey, "TCS_SHORT", finding=fid)
    waiting = _task(journey, "BOOKING_DATE_UNUSABLE", finding=fid)
    assert _enforce(journey)["task:finding_already_closed"] == 1
    assert _task_status(journey, task) == "CANCELLED"
    assert _task_status(journey, waiting) == "READY"  # its waiting is the ask
