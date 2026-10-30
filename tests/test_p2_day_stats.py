"""Day stats for the Employee and Team Lead dashboards (2026-10-30): from stored data only."""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import add_page, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_day_stats import build_day_stats
from audit_core.uc03_p2_object_keys import ist_day
from audit_core.uc03_p2_runtime import record_activity


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="daystats")
    try:
        yield created
    finally:
        with engine.begin() as c:
            set_tenant_context(c, created.tenant_id)
            c.execute(text("DELETE FROM auditcore.p2_tasks WHERE tenant_id=:t AND scope_kind <> 'JOURNEY'"),
                      {"t": created.tenant_id})
            c.execute(text("DELETE FROM auditcore.p2_gate_passes WHERE tenant_id=:t"), {"t": created.tenant_id})
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _today():
    return ist_day().date()


def _tl(journey) -> str:
    tl = f"tl-{uuid4().hex[:8]}"
    with journey.engine.begin() as c:
        c.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id) VALUES (:t, :a, 'TL', :d)"),
            {"t": journey.tenant_id, "a": tl, "d": journey.dealer_id},
        )
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text("UPDATE auditcore.journeys SET created_by_actor_id=:a, created_by_display_name='Pc One' "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"a": journey.actor_id, "t": journey.tenant_id, "j": journey.journey_id},
        )
    return tl


def _journey_task(journey, *, done: bool, late: bool = False, role: str = "PC"):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text("INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, "
                 "source_type, title, description, dedupe_key, assigned_role_code, task_status, completion_protocol, "
                 "due_at_utc, action_completed_at_utc, verified_at_utc) "
                 "VALUES (:t, :j, 'PC_UPLOAD_STATUS', 'DOCUMENT', 'SYSTEM', 'DOCUMENT', 'Upload again', 'Page 2', :k, "
                 ":role, :status, 'MACHINE_VERIFIED', "
                 "CASE WHEN :late THEN now() - interval '2 hours' ELSE now() + interval '2 hours' END, "
                 "CASE WHEN :done THEN now() END, CASE WHEN :done THEN now() END)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": f"k-{uuid4().hex}", "role": role,
             "status": "VERIFIED_COMPLETE" if done else "READY", "late": late, "done": done},
        )


def _tm_task(journey, *, assignee: str, completed_by: str | None, reviewer: str | None, verified: bool):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text("INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, scope_kind, dealer_id, outlet_id, task_type, "
                 "category, origin_kind, source_type, title, description, dedupe_key, assigned_role_code, "
                 "assigned_actor_id, reviewer_role_code, reviewer_actor_id, task_status, completion_protocol, "
                 "action_completed_at_utc, completed_by_actor_id, verified_at_utc) "
                 "VALUES (:t, NULL, 'OUTLET', :d, :o, 'TASK_MANAGER', 'TASK_MANAGER', 'HUMAN', 'TASK_MANAGER', "
                 "'Count the stock', 'Daily', :k, 'PC', :assignee, 'TL', :reviewer, :status, 'REQUESTER_CONFIRMED', "
                 "now(), :completed_by, CASE WHEN :verified THEN now() END)"),
            {"t": journey.tenant_id, "d": journey.dealer_id, "o": journey.outlet_id, "k": f"tm-{uuid4().hex}",
             "assignee": assignee, "reviewer": reviewer, "completed_by": completed_by, "verified": verified,
             "status": "VERIFIED_COMPLETE" if verified else "AWAITING_REQUESTER_REVIEW"},
        )


def _vehicle_gate_pass(journey, batch_di_document_id, delivery_date):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text("INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id, di_document_id, "
                 "document_type_key, evidence_purpose, process_area, association_status) "
                 "VALUES (:t, :j, :c, :s, :d, 'gate_pass', 'JOURNEY_DOCUMENT', 'DELIVERY', 'ACTIVE')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "c": journey.customer_id, "s": str(uuid4()),
             "d": str(batch_di_document_id)},
        )
        c.execute(
            text("INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id, di_document_id, "
                 "field_key, stage_code, source_document_type_key, extracted_value, effective_value) "
                 "VALUES (:t, :j, :d, 'delivery_date', 'DELIVERY', 'gate_pass', CAST(:v AS jsonb), CAST(:v AS jsonb))"),
            {"t": journey.tenant_id, "j": journey.journey_id, "d": str(batch_di_document_id),
             "v": json.dumps(delivery_date.isoformat())},
        )


def _daily_gate_pass(journey, *, deliveries: int, finished: bool):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text("INSERT INTO auditcore.p2_gate_passes (tenant_id, dealer_id, outlet_id, outlet_name, pc_actor_id, "
                 "pc_name, business_date, object_key, content_type, size_bytes, width_px, height_px, sharpness, "
                 "new_deliveries, stock_transfers, test_drives, other_count, counts_submitted_at_utc, completed_at_utc) "
                 "VALUES (:t, :d, :o, 'O', :a, 'Pc One', :day, 'k', 'image/jpeg', 10, 100, 100, 1, :n, 0, 0, 0, now(), "
                 "CASE WHEN :finished THEN now() END)"),
            {"t": journey.tenant_id, "d": journey.dealer_id, "o": journey.outlet_id, "a": journey.actor_id,
             "day": _today(), "n": deliveries, "finished": finished},
        )


def _stats(journey, actor, *, role=None, start=None, end=None):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return build_day_stats(c, tenant_id=journey.tenant_id, actor_id=actor, functional_role=role,
                               start=start or _today(), end=end or _today())


@pytest.mark.skipif(_today().isoweekday() == 7, reason="Sunday has no gate pass day")
def test_a_pc_sees_their_own_day(journey):
    tl = _tl(journey)
    # Two uploads while the journey is still a booking, then it goes into delivery and a third comes in.
    add_page(journey, page_number=1, status="READY")
    add_page(journey, page_number=2, status="READY")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        record_activity(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id, event_type="STAGE_CHANGED",
                        subject_type="JOURNEY", subject_id=str(journey.journey_id),
                        details={"from": "BOOKING_VERIFY_DOCUMENTS", "to": "BOOKING_COMPLETE"})
    _, _, gate_pass_doc = add_page(journey, page_number=3, status="READY")
    # The vehicle gate pass on that delivery upload is dated three days ago: backlog cleared today.
    _vehicle_gate_pass(journey, gate_pass_doc, _today() - timedelta(days=3))
    _journey_task(journey, done=True, late=True)
    _journey_task(journey, done=False)
    _tm_task(journey, assignee=journey.actor_id, completed_by=journey.actor_id, reviewer=tl, verified=True)
    _daily_gate_pass(journey, deliveries=2, finished=True)

    stats = _stats(journey, journey.actor_id)

    assert stats["caller"]["supervisor"] is False and stats["dealers"] == []
    up = stats["totals"]["uploads"]
    assert up == {"files": 3, "booking": 2, "delivery": 1, "journeys": 1}
    tk = stats["totals"]["tasks"]
    assert tk == {"closed": 2, "journey": 1, "taskManager": 1, "closedLate": 1}
    assert stats["totals"]["gatePass"] == {"days": 1, "uploaded": 1, "finished": 1, "missing": 0}
    d = stats["totals"]["deliveries"]
    assert d["asPerRegister"] == 2 and d["updatedInTool"] == 0
    assert d["pendingDueToPc"] == 2 and d["notNotifiedToPc"] == 0
    assert d["backlogCleared"] == 1
    assert [p["actorId"] for p in stats["pcs"]] == [journey.actor_id]
    assert stats["pcs"][0]["gatePass"]["uploaded"] == 1
    assert stats["self"]["uploads"]["files"] == 3 and stats["self"]["tasks"]["closed"] == 2
    assert stats["days"][0]["uploads"]["files"] == 3 and stats["days"][0]["deliveries"]["backlogCleared"] == 1
    assert len(stats["outlets"]) == 1 and stats["outlets"][0]["gatePassDays"][0]["status"] == "DONE"


@pytest.mark.skipif(_today().isoweekday() == 7, reason="Sunday has no gate pass day")
def test_a_team_lead_sees_the_outlet_and_dealership_and_their_own_reviews(journey):
    tl = _tl(journey)
    add_page(journey, page_number=1, status="READY")
    _journey_task(journey, done=True)
    _tm_task(journey, assignee=journey.actor_id, completed_by=journey.actor_id, reviewer=tl, verified=True)
    _tm_task(journey, assignee=journey.actor_id, completed_by=journey.actor_id, reviewer=tl, verified=False)
    _daily_gate_pass(journey, deliveries=1, finished=False)

    stats = _stats(journey, tl)

    assert stats["caller"]["supervisor"] is True and stats["caller"]["roles"] == ["TL"]
    assert len(stats["dealers"]) == 1
    dealer = stats["dealers"][0]
    # Both Task Manager tasks were completed by the PC today; one still awaits the TL's review,
    # which (as in the Daily Status) does not undo the PC's completion.
    assert dealer["uploads"]["files"] == 1 and dealer["tasks"] == {"closed": 3, "journey": 1, "taskManager": 2,
                                                                  "closedLate": 0}
    assert dealer["gatePass"] == {"days": 1, "uploaded": 1, "finished": 0, "missing": 0}
    outlet = dealer["outlets"][0]
    assert outlet["gatePassApplicable"] is True
    assert outlet["pcs"][0]["actorId"] == journey.actor_id
    assert outlet["pcs"][0]["uploads"]["files"] == 1 and outlet["pcs"][0]["tasks"]["closed"] == 3
    assert outlet["gatePassDays"][0]["status"] == "ANSWER_PENDING"
    # The TL closed nothing themselves; they reviewed one Task Manager task.
    assert stats["self"]["tasks"]["closed"] == 0 and stats["self"]["tasksReviewed"] == 1
    assert stats["self"]["journeyTasksWorked"] == 0
    assert [p["actorId"] for p in stats["pcs"]] == [journey.actor_id]


def test_a_range_lists_every_day_and_refuses_too_many(journey):
    _tl(journey)
    start = _today() - timedelta(days=6)
    stats = _stats(journey, journey.actor_id, start=start, end=_today())
    assert [d["date"] for d in stats["days"]] == [(start + timedelta(days=i)).isoformat() for i in range(7)]
    assert stats["from"] == start.isoformat() and stats["to"] == _today().isoformat()


def test_the_endpoint_answers_and_refuses_too_long_a_range(journey):
    from fastapi.testclient import TestClient
    from p2_support import AllowAllAuthorization

    from audit_core.dependencies import get_human_principal
    from audit_core.main import app
    from audit_core.security import HumanPrincipal
    from audit_core.security_authorization import get_security_authorization_client

    _tl(journey)
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        client = TestClient(app, raise_server_exceptions=False)
        base = f"/p2/v1/tenants/{journey.tenant_id}/reports/day-stats"
        ok = client.get(base)
        assert ok.status_code == 200, ok.text
        assert ok.json()["from"] == _today().isoformat() and ok.json()["caller"]["actorId"] == journey.actor_id
        too_long = client.get(base, params={"from": (_today() - timedelta(days=40)).isoformat()})
        assert too_long.status_code == 422
    finally:
        app.dependency_overrides.clear()
