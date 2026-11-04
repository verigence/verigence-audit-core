"""PC Home and Tracker (my-work), the check-out check, and a Reviewer closing the Team
Lead's Journey tasks (product owner 2026-11-02; real Postgres)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import test_uc03_p2_gate_pass as gate_pass_tests
from sqlalchemy import text

from audit_core import uc03_p2_my_work as my_work
from audit_core.db import set_tenant_context
from audit_core.task_closure_roles import (
    can_close_as,
    has_team_lead_authority,
    roles_closable_by,
)
from audit_core.task_manager import gate_pass_day_label, previous_gate_pass_day
from audit_core.uc03_p2_object_keys import ist_day
from audit_core.uc03_p2_tasks import QUALITY_TASK, TaskPermissionError, submit_action

_delivery_file, _register, _upload = (gate_pass_tests._delivery_file, gate_pass_tests._register,
                                     gate_pass_tests._upload)
setup = gate_pass_tests.setup  # the Daily Gate Pass fixture: a PC, an outlet, a Journey, storage doubled


def _work(s, **params):
    base = f"/p2/v1/tenants/{s.journey.tenant_id}/my-work"
    return s.client.get(base, params=params)


def _add_role(s, actor: str, role: str, *, dealer=True, outlet=False):
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id, outlet_id) VALUES (:t, :a, :r, :d, :o)"),
            {"t": s.journey.tenant_id, "a": actor, "r": role,
             "d": s.journey.dealer_id if dealer else None, "o": s.journey.outlet_id if outlet else None})


def _journey_task(s, *, role: str, task_type: str = "FINDING_REVIEW") -> str:
    task_id = str(uuid4())
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(
            text("""
                INSERT INTO auditcore.p2_tasks (tenant_id, task_id, root_task_id, journey_id, task_type, category,
                  origin_kind, source_type, dedupe_key, title, description, assigned_role_code, allowed_actions,
                  completion_protocol)
                VALUES (:t, :id, :id, :j, :type, 'FINDING_REMEDIATION', 'SYSTEM', 'RULE', :dk, 'Finding',
                  'Check the finding', :role, CAST(:allowed AS jsonb), 'MACHINE_VERIFIED')
            """),
            {"t": s.journey.tenant_id, "id": task_id, "j": str(s.journey.journey_id), "type": task_type,
             "dk": f"test:{task_id}", "role": role,
             "allowed": json.dumps(["ACCEPT_EXCEPTION", "ADD_COMMENT", "CONFIRM_BREACH", "MARK_FALSE_POSITIVE"])})
    return task_id


# --------------------------------------------------------------------------- the rule itself

def test_the_closure_rule():
    assert can_close_as("TL", "TL") and can_close_as("TL", "REVIEWER")
    # A Reviewer closes the PC's tasks too (product owner 2026-11-04).
    assert can_close_as("PC", "REVIEWER")
    assert not can_close_as("TL", "PC") and not can_close_as("REVIEWER", "TL") and not can_close_as("REVIEWER", "PC")
    assert roles_closable_by("REVIEWER") == ["REVIEWER", "PC", "TL"] and roles_closable_by("PC") == ["PC"]
    assert has_team_lead_authority("REVIEWER", "TL") and not has_team_lead_authority("REVIEWER", "PC")
    assert has_team_lead_authority("PM", "PC")


def test_a_reviewer_closes_a_team_lead_journey_task_but_not_a_pcs(setup):
    s = setup
    tl_task = _journey_task(s, role="TL")
    pc_task = _journey_task(s, role="PC")
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        with pytest.raises(TaskPermissionError):
            submit_action(connection, tenant_id=s.journey.tenant_id, task_id=tl_task, action="ACCEPT_EXCEPTION",
                          actor_id="a-pc", actor_role_code="PC", comment="ok")
        with pytest.raises(TaskPermissionError):
            submit_action(connection, tenant_id=s.journey.tenant_id, task_id=pc_task, action="ACCEPT_EXCEPTION",
                          actor_id="a-reviewer", actor_role_code="REVIEWER", comment="ok")
        done = submit_action(connection, tenant_id=s.journey.tenant_id, task_id=tl_task, action="ACCEPT_EXCEPTION",
                             actor_id="a-reviewer", actor_role_code="REVIEWER", comment="Scheme letter on file")
    assert done["status"] == "VERIFIED_COMPLETE" and done["outcome"] == "EXCEPTION_ACCEPTED"


def test_the_reviewers_task_queue_carries_the_team_leads_and_pcs_tasks(setup):
    s = setup
    tl_task = _journey_task(s, role="TL")
    pc_task = _journey_task(s, role="PC")
    queue = s.client.get(f"/p2/v1/tenants/{s.journey.tenant_id}/tasks",
                         params={"role": "REVIEWER", "includeLegacy": "false"})
    assert queue.status_code == 200, queue.text
    ids = {str(t.get("task_id") or t.get("taskId")) for t in queue.json()["items"]}
    assert tl_task in ids and pc_task in ids  # the PC's too, from 2026-11-04
    tl_queue = s.client.get(f"/p2/v1/tenants/{s.journey.tenant_id}/tasks",
                            params={"role": "TL", "includeLegacy": "false"}).json()
    assert tl_task in {str(t.get("task_id") or t.get("taskId")) for t in tl_queue["items"]}


# --------------------------------------------------------------------------- PC Home / Tracker

def test_pc_home_counts_uploads_gate_register_and_tasks(setup):
    s = setup
    _delivery_file(s, journeys=2)                                   # two deliveries of today's gate pass
    gp = _upload(s).json()["gatePassId"]
    _register(s, gp, 5, 3)                                          # 2 uploaded, 1 pending, 2 not intimated
    with s.journey.engine.begin() as connection:                    # the PC opened the booking (every live one has its creator)
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("UPDATE auditcore.journeys SET created_by_actor_id=:a WHERE tenant_id=:t AND journey_id=:j"),
                           {"a": s.journey.actor_id, "t": s.journey.tenant_id, "j": str(s.journey.journey_id)})
    _journey_task(s, role="PC")
    today = ist_day().date()
    body = _work(s, **{"from": today.isoformat(), "to": today.isoformat()})
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["role"] == "PC"
    [line] = data["outlets"]
    assert line["outletId"] == str(s.journey.outlet_id)
    assert line["deliveriesUpdated"] + line["bookingsUploaded"] == line["journeysUploaded"] >= 1
    assert line["journeysTotal"] >= 1
    assert line["tasksPending"] == 1
    if today.isoweekday() != 7:
        [day] = line["gateRegister"]
        assert (day["status"], day["total"], day["intimated"], day["uploadedInTool"], day["pendingWithPc"],
                day["notIntimated"]) == ("DONE", 5, 3, 2, 1, 2)
        assert line["pendingDeliveryFiles"] == 1 and line["gateRegisterUploaded"] == 1
        assert day["gatePassId"] == gp and day["pages"] >= 1 and day["uploadedAtUtc"]
    assert data["totals"]["tasksPending"] == 1


def test_a_range_of_up_to_92_days_is_allowed(setup):
    s = setup
    today = ist_day().date()
    week = _work(s, **{"from": (today - timedelta(days=6)).isoformat(), "to": today.isoformat()}).json()
    assert len(week["outlets"][0]["gateRegister"]) == sum(
        1 for i in range(7) if (today - timedelta(days=i)).isoweekday() != 7)
    assert _work(s, **{"from": (today - timedelta(days=40)).isoformat(), "to": today.isoformat()}).status_code == 200
    assert _work(s, **{"from": (today - timedelta(days=100)).isoformat(), "to": today.isoformat()}).status_code == 422


def test_a_gate_register_past_its_due_time_is_missed(setup):
    s = setup
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        outlets = my_work._my_outlets(connection, s.journey.tenant_id, s.journey.actor_id, "PC")
        today = ist_day().date()
        days = my_work.gate_register(connection, s.journey.tenant_id, outlets, today - timedelta(days=6), today,
                                     now=datetime.now(UTC) + timedelta(days=10))
    statuses = {d["status"] for d in days[str(s.journey.outlet_id)]}
    assert statuses == {"MISSED"}


def test_the_reviewer_tracker(setup):
    s = setup
    reviewer = f"rv-{uuid4().hex[:6]}"
    _add_role(s, reviewer, "REVIEWER")
    tl_task = _journey_task(s, role="TL")
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_stage_document_reviews (tenant_id, journey_id, stage_code, "
                 "reviewed_by_actor_id, reviewed_by_role_code, document_count, dealer_id, outlet_id) "
                 "VALUES (:t, :j, 'BOOKING', :a, 'REVIEWER', 3, :d, :o)"),
            {"t": s.journey.tenant_id, "j": str(s.journey.journey_id), "a": reviewer,
             "d": s.journey.dealer_id, "o": s.journey.outlet_id})
        submit_action(connection, tenant_id=s.journey.tenant_id, task_id=tl_task, action="ACCEPT_EXCEPTION",
                      actor_id=reviewer, actor_role_code="REVIEWER", comment="Documented")
    _journey_task(s, role="TL")                                     # still open: pending for the Reviewer
    s.acting["subject"] = reviewer
    data = _work(s).json()
    assert data["role"] == "REVIEWER"
    [line] = data["outlets"]
    assert (line["journeysWorked"], line["reviewsClosed"], line["tasksClosed"], line["tasksPending"]) == (1, 1, 1, 1)
    [today] = data["periods"]                                          # the same work, on today's row
    assert (today["totals"]["journeysWorked"], today["totals"]["reviewsClosed"], today["totals"]["tasksClosed"]) == (1, 1, 1)


# --------------------------------------------------------------------------- check-out

def test_checkout_check_names_the_unfinished_n_minus_1_registers_of_onsite_outlets(setup):
    s = setup
    day = previous_gate_pass_day(ist_day().date())
    check = s.client.get(f"/p2/v1/tenants/{s.journey.tenant_id}/my-work/checkout-check").json()
    assert check["day"] == day.isoformat() and check["label"] == gate_pass_day_label(day)
    assert [p["outletId"] for p in check["pending"]] == [str(s.journey.outlet_id)]
    # Finished for that day: nothing to warn about.
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_gate_passes (tenant_id, gate_pass_id, dealer_id, outlet_id, outlet_name, "
                 "pc_actor_id, pc_name, business_date, object_key, content_type, size_bytes, width_px, height_px, "
                 "sharpness, completed_at_utc, counts_submitted_at_utc, new_deliveries) "
                 "VALUES (:t, :id, :d, :o, 'X', :a, 'PC', :day, 'k', 'image/jpeg', 1, 1, 1, 1, now(), now(), 0)"),
            {"t": s.journey.tenant_id, "id": str(uuid4()), "d": s.journey.dealer_id, "o": s.journey.outlet_id,
             "a": s.journey.actor_id, "day": day})
    assert s.client.get(f"/p2/v1/tenants/{s.journey.tenant_id}/my-work/checkout-check").json()["pending"] == []


def test_checkout_check_leaves_satellite_outlets_out(setup):
    s = setup
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("UPDATE auditcore.dealer_outlets SET outlet_classification='SATELLITE' "
                                "WHERE outlet_id=:o"), {"o": s.journey.outlet_id})
    assert s.client.get(f"/p2/v1/tenants/{s.journey.tenant_id}/my-work/checkout-check").json()["pending"] == []


def test_a_satellite_has_no_gate_register_day_before_3_november(setup):
    s = setup
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("UPDATE auditcore.dealer_outlets SET outlet_classification='SATELLITE' "
                                "WHERE outlet_id=:o"), {"o": s.journey.outlet_id})
        outlets = my_work._my_outlets(connection, s.journey.tenant_id, s.journey.actor_id, "PC")
        from datetime import date
        days = my_work.gate_register(connection, s.journey.tenant_id, outlets, date(2026, 10, 30), date(2026, 11, 4))
    assert [d["date"] for d in days[str(s.journey.outlet_id)]] == ["2026-11-03", "2026-11-04"]


# --------------------------------------------------------------------------- the Tracker, day by day (2026-11-03)

def test_the_tracker_is_day_by_day_and_counts_journeys_completed(setup):
    """Each day of the period has its own row per outlet, newest first; a delivery file closed
    today (every mandatory document in, no PC document task open) is a journey completed today."""
    s = setup
    _delivery_file(s)                                               # a delivery page uploaded today
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("UPDATE auditcore.journeys SET created_by_actor_id=:a WHERE tenant_id=:t AND journey_id=:j"),
                           {"a": s.journey.actor_id, "t": s.journey.tenant_id, "j": str(s.journey.journey_id)})
        connection.execute(
            text("INSERT INTO auditcore.p2_stage_gate_state (tenant_id, journey_id, stage_code, gate_key, "
                 "gate_status, details) VALUES (:t, :j, 'DELIVERY', 'REQUIRED_DOCUMENTS', 'PASS', '{\"missing\": []}')"),
            {"t": s.journey.tenant_id, "j": str(s.journey.journey_id)})
    today = ist_day().date()
    data = _work(s, **{"from": (today - timedelta(days=6)).isoformat(), "to": today.isoformat()}).json()
    assert [d["date"] for d in data["periods"]] == [(today - timedelta(days=i)).isoformat() for i in range(7)]
    first = data["periods"][0]
    [line] = first["outlets"]
    assert line["outletId"] == str(s.journey.outlet_id)
    assert line["deliveriesUpdated"] == 1 and line["journeysCompleted"] == 1
    assert first["totals"]["journeysCompleted"] == 1
    assert all(d["totals"]["journeysUploaded"] == 0 for d in data["periods"][1:])
    assert data["outlets"][0]["journeysCompleted"] == 1 and data["totals"]["journeysCompleted"] == 1
    # A Sunday has no gate register day; any other day carries the outlet's entry.
    for d in data["periods"]:
        entry = d["outlets"][0]["gateRegister"]
        assert (entry is None) == (datetime.fromisoformat(d["date"]).isoweekday() == 7)


def test_a_journey_with_a_document_task_open_is_not_completed(setup):
    s = setup
    _delivery_file(s)
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("UPDATE auditcore.journeys SET created_by_actor_id=:a WHERE tenant_id=:t AND journey_id=:j"),
                           {"a": s.journey.actor_id, "t": s.journey.tenant_id, "j": str(s.journey.journey_id)})
        connection.execute(
            text("INSERT INTO auditcore.p2_stage_gate_state (tenant_id, journey_id, stage_code, gate_key, "
                 "gate_status, details) VALUES (:t, :j, 'DELIVERY', 'REQUIRED_DOCUMENTS', 'PASS', '{\"missing\": []}')"),
            {"t": s.journey.tenant_id, "j": str(s.journey.journey_id)})
    _journey_task(s, role="PC", task_type="DOCUMENT_MISSING")
    today = ist_day().date()
    data = _work(s, **{"from": today.isoformat(), "to": today.isoformat()}).json()
    assert data["totals"]["journeysCompleted"] == 0 and data["periods"][0]["totals"]["journeysCompleted"] == 0


def test_a_month_is_allowed(setup):
    s = setup
    today = ist_day().date()
    month = _work(s, **{"from": (today - timedelta(days=30)).isoformat(), "to": today.isoformat()})
    assert month.status_code == 200, month.text
    assert len(month.json()["periods"]) == 31


def test_rows_by_week_and_by_month():
    """Weeks run Monday to Sunday and months are calendar months, cut to the range, newest first."""
    from datetime import date
    weeks = my_work.buckets(date(2026, 10, 14), date(2026, 11, 3), "week")
    assert [(a.isoformat(), b.isoformat()) for a, b, _ in weeks] == [
        ("2026-11-02", "2026-11-03"), ("2026-10-26", "2026-11-01"), ("2026-10-19", "2026-10-25"),
        ("2026-10-14", "2026-10-18")]
    months = my_work.buckets(date(2026, 9, 1), date(2026, 11, 3), "month")
    assert [(a.isoformat(), b.isoformat(), label) for a, b, label in months] == [
        ("2026-11-01", "2026-11-03", "1 Nov – 3 Nov 2026"), ("2026-10-01", "2026-10-31", "October 2026"),
        ("2026-09-01", "2026-09-30", "September 2026")]
    assert len(my_work.buckets(date(2026, 10, 1), date(2026, 10, 31), "day")) == 31


def test_the_tracker_by_week_sums_the_days(setup):
    s = setup
    _delivery_file(s)
    today = ist_day().date()
    by_day = _work(s, **{"from": (today - timedelta(days=13)).isoformat(), "to": today.isoformat()}).json()
    by_week = _work(s, **{"from": (today - timedelta(days=13)).isoformat(), "to": today.isoformat(),
                          "by": "week"}).json()
    assert by_week["by"] == "week" and 2 <= len(by_week["periods"]) <= 3
    assert sum(p["totals"]["journeysUploaded"] for p in by_week["periods"]) == \
        sum(p["totals"]["journeysUploaded"] for p in by_day["periods"]) == 1
    days_with_register = sum(p["totals"]["gateRegisterDays"] for p in by_day["periods"])
    assert sum(p["totals"]["gateRegisterDays"] for p in by_week["periods"]) == days_with_register
    assert _work(s, by="year").status_code == 400


def test_a_reviewer_closes_a_pcs_task(setup):
    """Product owner 2026-11-04: "reviewer should be able to close the PC tasks"."""
    s = setup
    pc_task = _journey_task(s, role="PC", task_type=QUALITY_TASK)
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_tasks SET allowed_actions=CAST(:a AS jsonb) "
                                "WHERE tenant_id=:t AND task_id=:id"),
                           {"a": json.dumps(["CLOSE_TASK", "ADD_COMMENT"]), "t": s.journey.tenant_id, "id": pc_task})
        done = submit_action(connection, tenant_id=s.journey.tenant_id, task_id=pc_task, action="CLOSE_TASK",
                             actor_id="a-reviewer", actor_role_code="REVIEWER", comment="Clear copy is on file")
    assert done["status"] == "VERIFIED_COMPLETE"
