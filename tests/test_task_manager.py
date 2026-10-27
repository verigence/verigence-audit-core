"""Task Manager (product owner 2026-10-26, docs/TASK_MANAGER_DESIGN_v1.0.md):
who may assign to whom, the one-off and recurring lifecycles, Sunday rules,
Missed marks, the Daily Gate Pass task, daily status, time on Verigence and
the Executive / Superadmin dashboard (real Postgres)."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import AllowAllAuthorization, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import task_manager as tm
from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_human_principal,
    require_super_admin_request,
)
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.security_integration import SecurityAdminContext
from audit_core.task_manager_reports import (
    build_task_status,
    build_utilisation,
    record_activity,
)

IST = tm.IST
MONDAY = date(2026, 11, 2)  # a Monday


def _at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=IST).astimezone(UTC)


@pytest.fixture
def project():
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="tm")
    t = journey.tenant_id
    people = {"PC": journey.actor_id}
    with engine.begin() as connection:
        for role in ("TL", "PM", "CRM", "REVIEWER", "EXECUTIVE"):
            actor = f"tm-{role.lower()}-{uuid4().hex[:6]}"
            people[role] = actor
            connection.execute(
                text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                     "VALUES (:t, :a, :r)"), {"t": t, "a": actor, "r": role})
        pc2 = f"tm-pc2-{uuid4().hex[:6]}"
        people["PC2"] = pc2
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id, outlet_id) VALUES (:t, :a, 'PC', :d, :o)"),
            {"t": t, "a": pc2, "d": journey.dealer_id, "o": journey.outlet_id})
    try:
        yield SimpleNamespace(engine=engine, tenant=t, journey=journey, people=people)
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, t)
        engine.dispose()


def _caller(p, role: str) -> tm.Caller:
    if role == tm.SUPER_ADMIN:
        return tm.Caller(actor_id="super-admin-1", role=tm.SUPER_ADMIN)
    return tm.Caller(actor_id=p.people[role], role=role if role != "PC2" else "PC")


def _db(p):
    class _Ctx:
        def __enter__(self):
            self.cm = p.engine.begin()
            self.c = self.cm.__enter__()
            set_tenant_context(self.c, p.tenant)
            return self.c

        def __exit__(self, *exc):
            return self.cm.__exit__(*exc)
    return _Ctx()


def _raise(p, raiser: str, to: str, minutes: int = 120, now: datetime | None = None) -> str:
    with _db(p) as c:
        [task_id] = tm.raise_tasks(c, tenant_id=p.tenant, caller=_caller(p, raiser), title="Check stock register",
                                   description="Count the vehicles and reconcile.", assignee_actor_ids=[p.people[to]],
                                   expected_close_minutes=minutes, now=now)
    return task_id


def _act(p, role: str, task_id: str, action: str, comment: str | None = "done", **details):
    with _db(p) as c:
        return tm.act(c, tenant_id=p.tenant, caller=_caller(p, role), task_id=task_id, action=action,
                      comment=comment, details=details)


# --------------------------------------------------------------------------- matrix


@pytest.mark.parametrize(("raiser", "to", "allowed"), [
    ("TL", "PC", True), ("TL", "CRM", True), ("TL", "REVIEWER", True), ("TL", "PM", False),
    ("PM", "TL", True), ("PM", "EXECUTIVE", False),
    ("CRM", "TL", True), ("CRM", "PM", True), ("CRM", "PC", False),
    ("REVIEWER", "PC", True), ("REVIEWER", "TL", False),
    ("EXECUTIVE", "PM", True), (tm.SUPER_ADMIN, "EXECUTIVE", True),
    ("PC", "TL", False), ("PC", "PC2", False),
])
def test_assign_matrix(project, raiser, to, allowed):
    if allowed:
        assert _raise(project, raiser, to)
    else:
        with pytest.raises(tm.TaskManagerForbidden):
            _raise(project, raiser, to)


def test_expected_time_to_close_is_required(project):
    with pytest.raises(tm.TaskManagerError), _db(project) as c:
        tm.raise_tasks(c, tenant_id=project.tenant, caller=_caller(project, "TL"), title="x", description="y",
                       assignee_actor_ids=[project.people["PC"]], expected_close_minutes=0)


# --------------------------------------------------------------------------- one-off lifecycle


def test_one_off_round_trip_with_send_back(project):
    p = project
    task_id = _raise(p, "TL", "PC", minutes=120)
    with _db(p) as c:
        mine = tm.list_tasks(c, tenant_id=p.tenant, caller=_caller(p, "PC"), view="mine")
        raised = tm.list_tasks(c, tenant_id=p.tenant, caller=_caller(p, "TL"), view="raised")
        other_pc = tm.list_tasks(c, tenant_id=p.tenant, caller=_caller(p, "PC2"), view="mine")
    [item] = mine["items"]
    assert item["myActions"] == ["START", "COMPLETE_ACTION", "ADD_COMMENT"]
    assert item["reviewerRole"] == "TL" and item["completionProtocol"] == "REQUESTER_CONFIRMED"
    assert [i["taskId"] for i in raised["items"]] == [task_id] and other_pc["items"] == []
    assert mine["counts"]["ACTION"] == 1

    with pytest.raises(tm.TaskManagerError):
        _act(p, "PC", task_id, "COMPLETE_ACTION", comment=None)  # a comment is required
    done = _act(p, "PC", task_id, "COMPLETE_ACTION", comment="Counted 14, register matches")
    assert done["status"] == "AWAITING_REQUESTER_REVIEW" and done["reviewDueAtUtc"]
    with pytest.raises(tm.TaskManagerForbidden):
        _act(p, "PC", task_id, "ACCEPT")  # the assignee cannot close it
    back = _act(p, "TL", task_id, "REJECT", comment="Attach the register photo")
    assert back["status"] == "RETURNED" and back["sendBacks"] == 1
    _act(p, "PC", task_id, "COMPLETE_ACTION", comment="Photo attached")
    closed = _act(p, "TL", task_id, "ACCEPT", comment=None)
    assert closed["status"] == "VERIFIED_COMPLETE" and closed["myActions"] == []
    assert [e["eventType"] for e in closed["events"]] == [
        "RAISED", "COMPLETE_ACTION", "REJECT", "COMPLETE_ACTION", "ACCEPT"]


def test_task_to_a_pm_has_no_reviewer(project):
    task_id = _raise(project, "EXECUTIVE", "PM")
    closed = _act(project, "PM", task_id, "COMPLETE_ACTION", comment="Done")
    assert closed["completionProtocol"] == "SELF_CLOSING" and closed["status"] == "VERIFIED_COMPLETE"


def test_reassign_and_cancel(project):
    p = project
    task_id = _raise(p, "TL", "PC")
    moved = _act(p, "TL", task_id, "REASSIGN", comment="PC on leave", assigneeActorId=p.people["PC2"])
    assert moved["assignedActorId"] == p.people["PC2"]
    with _db(p) as c:
        assert tm.list_tasks(c, tenant_id=p.tenant, caller=_caller(p, "PC"), view="mine")["items"] == []
    with pytest.raises(tm.TaskManagerForbidden):
        _act(p, "TL", task_id, "REASSIGN", comment="x", assigneeActorId=p.people["PM"])  # TL cannot assign a PM
    cancelled = _act(p, "TL", task_id, "CANCEL", comment="No longer needed")
    assert cancelled["status"] == "CANCELLED"
    with pytest.raises(tm.TaskManagerConflict):
        _act(p, "PC2", task_id, "COMPLETE_ACTION")


# --------------------------------------------------------------------------- recurring


def _schedule(**over):
    base = {"starts_on": date(2026, 1, 1), "ends_on": None, "skip_dates": [], "weekdays": [1, 2, 3, 4, 5, 6],
            "frequency": "DAILY", "month_day": None}
    return {**base, **over}


def test_sunday_rules():
    daily = _schedule(weekdays=[1, 2, 3, 4, 5, 6, 7])
    assert not tm.occurs_on(daily, MONDAY - timedelta(days=1))  # never a daily task on Sunday
    assert tm.occurs_on(daily, MONDAY)
    weekly_sunday = _schedule(frequency="WEEKLY", weekdays=[7])
    assert not tm.occurs_on(weekly_sunday, MONDAY - timedelta(days=1))
    assert tm.occurs_on(weekly_sunday, MONDAY)  # moved to the Monday
    # 1 Nov 2026 is a Sunday: the monthly 1st moves to Monday 2 Nov.
    monthly = _schedule(frequency="MONTHLY", month_day=1)
    assert not tm.occurs_on(monthly, date(2026, 11, 1)) and tm.occurs_on(monthly, date(2026, 11, 2))
    # 31st in a 30-day month falls back to the 30th.
    assert tm.occurs_on(_schedule(frequency="MONTHLY", month_day=31), date(2026, 9, 30))
    assert not tm.occurs_on(_schedule(skip_dates=[MONDAY]), MONDAY)


def test_recurring_needs_executive_and_is_reviewed_by_pm(project):
    p = project
    spec = {"title": "Daily stock check", "description": "Count stock", "frequency": "DAILY",
            "expectedCloseMinutes": 240, "assigneeMode": "ROLE_EACH", "assignedRole": "PC",
            "runTimeIst": "09:00", "startsOn": "2026-01-01"}
    with pytest.raises(tm.TaskManagerForbidden), _db(p) as c:
        tm.create_schedule(c, tenant_id=p.tenant, caller=_caller(p, "PM"), spec=spec)
    with _db(p) as c:
        sched = tm.create_schedule(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), spec=spec)
        assert sched["weekdays"] == [1, 2, 3, 4, 5, 6]
        # Before 09:00 nothing (the gate pass schedule seeded now starts tomorrow); at 09:05 one task
        # per PC; a second pass raises nothing more.
        assert tm.run_tick(c, tenant_id=p.tenant, now=_at(MONDAY, 8, 0))["raised"] == 0
        assert tm.run_tick(c, tenant_id=p.tenant, now=_at(MONDAY, 9, 5))["raised"] == 2
        assert tm.run_tick(c, tenant_id=p.tenant, now=_at(MONDAY, 9, 10))["raised"] == 0
        tasks = tm.list_tasks(c, tenant_id=p.tenant, caller=_caller(p, "PC"), view="mine", now=_at(MONDAY, 10))
    [task] = [t for t in tasks["items"] if t["kind"] == "RECURRING"]
    assert task["reviewerRole"] == "PM" and task["dueAtUtc"] == _at(MONDAY, 13).isoformat()
    _act(p, "PC", task["taskId"], "COMPLETE_ACTION", comment="Counted")
    with pytest.raises((tm.TaskManagerForbidden, tm.TaskManagerNotFound)):
        _act(p, "TL", task["taskId"], "ACCEPT")  # only a PM closes a recurring task
    with pytest.raises(tm.TaskManagerForbidden):
        _act(p, "EXECUTIVE", task["taskId"], "ACCEPT")  # the Executive sees it but the PM reviews
    assert _act(p, "PM", task["taskId"], "ACCEPT")["status"] == "VERIFIED_COMPLETE"

    with _db(p) as c:
        # Tuesday's occurrence marks the other PC's Monday task Missed; Sunday raises nothing.
        tm.run_tick(c, tenant_id=p.tenant, now=_at(MONDAY + timedelta(days=1), 9, 5))
        missed = c.execute(text("SELECT count(*) FROM auditcore.p2_tasks WHERE tenant_id=:t AND missed_at_utc "
                                "IS NOT NULL AND occurrence_date=:d"), {"t": p.tenant, "d": MONDAY}).scalar_one()
        assert missed == 1
        assert tm.run_tick(c, tenant_id=p.tenant, now=_at(MONDAY + timedelta(days=6), 9, 5))["raised"] == 0


def test_recurring_review_falls_to_executive_without_a_pm(project):
    p = project
    with _db(p) as c:
        c.execute(text("UPDATE auditcore.business_assignments SET assignment_status='INACTIVE' "
                       "WHERE tenant_id=:t AND security_actor_id=:a"), {"t": p.tenant, "a": p.people["PM"]})
        tm.create_schedule(c, tenant_id=p.tenant, caller=_caller(p, tm.SUPER_ADMIN), spec={
            "title": "Weekly MIS", "description": "Send MIS", "frequency": "WEEKLY", "weekdays": [1],
            "expectedCloseMinutes": 60, "assigneeMode": "ACTOR", "assignedActorId": p.people["TL"],
            "startsOn": "2026-01-01"})
        tm.run_tick(c, tenant_id=p.tenant, now=_at(MONDAY, 10))
        row = c.execute(text("SELECT reviewer_role_code FROM auditcore.p2_tasks WHERE tenant_id=:t "
                             "AND task_type='TM_RECURRING'"), {"t": p.tenant}).scalar_one()
    assert row == "EXECUTIVE"


def test_skip_a_holiday(project):
    p = project
    with _db(p) as c:
        sched = tm.create_schedule(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), spec={
            "title": "Daily", "description": "d", "frequency": "DAILY", "expectedCloseMinutes": 60,
            "assigneeMode": "ACTOR", "assignedActorId": p.people["TL"], "startsOn": "2026-01-01"})
        future = tm.ist_today() + timedelta(days=10)
        tm.skip_occurrence(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"),
                           schedule_id=sched["scheduleId"], day=future, reason="Diwali")
        rows = tm.list_schedules(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"))
    row = next(r for r in rows if r["scheduleId"] == sched["scheduleId"])
    assert future.isoformat() in row["skipDates"]


# --------------------------------------------------------------------------- gate pass


def _gate_pass(p, day: date, *, finished: bool) -> None:
    with _db(p) as c:
        c.execute(text("""
            INSERT INTO auditcore.p2_gate_passes (tenant_id, dealer_id, outlet_id, outlet_name, pc_actor_id, pc_name,
              business_date, object_key, content_type, size_bytes, width_px, height_px, sharpness, completed_at_utc)
            VALUES (:t, :d, :o, 'O', :a, 'Pc One', :day, 'k', 'image/jpeg', 10, 100, 100, 50, :done)
        """), {"t": p.tenant, "d": p.journey.dealer_id, "o": p.journey.outlet_id, "a": p.people["PC"],
               "day": day, "done": _at(day, 18) if finished else None})


def test_gate_pass_task_is_system_reviewed(project):
    p = project
    with _db(p) as c:
        tm.ensure_system_schedules(c, tenant_id=p.tenant, now=_at(MONDAY, 7, 0))
        assert tm.run_tick(c, tenant_id=p.tenant, now=_at(MONDAY, 7, 5))["raised"] == 0  # starts the next day
        c.execute(text("UPDATE auditcore.p2_task_schedules SET starts_on = :d WHERE tenant_id = :t"),
                  {"d": MONDAY, "t": p.tenant})
        c.execute(text("DELETE FROM auditcore.p2_scheduled_runs WHERE tenant_id = :t"), {"t": p.tenant})
        assert tm.run_tick(c, tenant_id=p.tenant, now=_at(MONDAY, 7, 5))["raised"] == 1  # one outlet with PCs
        mine = tm.list_tasks(c, tenant_id=p.tenant, caller=_caller(p, "PC2"), view="mine")
    [task] = mine["items"]
    assert task["kind"] == "GATE_PASS" and task["completionProtocol"] == "SYSTEM_VERIFIED"
    assert task["myActions"] == ["ADD_COMMENT"]  # no human closes it
    assert task["dueAtUtc"] == datetime.combine(MONDAY + timedelta(days=1), time(23, 59, 59),
                                                tzinfo=IST).astimezone(UTC).isoformat()
    _gate_pass(p, MONDAY, finished=True)
    with _db(p) as c:
        assert tm.run_tick(c, tenant_id=p.tenant, now=_at(MONDAY, 19))["gatePassClosed"] == 1

    # Tuesday: no gate pass at all -> incomplete after 23:59 Wednesday.
    tuesday = MONDAY + timedelta(days=1)
    with _db(p) as c:
        tm.run_tick(c, tenant_id=p.tenant, now=_at(tuesday, 7, 5))
        assert tm.run_tick(c, tenant_id=p.tenant, now=_at(tuesday + timedelta(days=1), 23, 0))["gatePassIncomplete"] == 0
        assert tm.run_tick(c, tenant_id=p.tenant, now=_at(tuesday + timedelta(days=2), 0, 5))["gatePassIncomplete"] == 1
        status = build_task_status(c, tenant_id=p.tenant, day=tuesday + timedelta(days=1),
                                   now=_at(tuesday + timedelta(days=2), 0, 10))
    assert status["taskManager"]["totals"]["incomplete"] == 1
    assert status["taskManager"]["flags"][0]["flags"] == ["INCOMPLETE"]


# --------------------------------------------------------------------------- daily status


def test_daily_status_flags_overdue_and_review_overdue(project):
    p = project
    raised_at = datetime.now(UTC) - timedelta(days=3)
    late = _raise(p, "TL", "PC", minutes=60, now=raised_at)
    review = _raise(p, "TL", "PC2", minutes=60 * 24 * 10)
    with _db(p) as c:
        c.execute(text("UPDATE auditcore.p2_tasks SET created_at_utc=:at WHERE tenant_id=:t AND task_id=:id"),
                  {"at": raised_at, "t": p.tenant, "id": late})
    _act(p, "PC2", review, "COMPLETE_ACTION", comment="done")
    with _db(p) as c:
        c.execute(text("UPDATE auditcore.p2_tasks SET review_due_at_utc = now() - interval '1 hour' "
                       "WHERE tenant_id=:t AND task_id=:id"), {"t": p.tenant, "id": review})
        status = build_task_status(c, tenant_id=p.tenant, day=tm.ist_today())
    section = status["taskManager"]
    assert section["totals"]["overdue"] == 1 and section["totals"]["reviewOverdue"] == 1
    flags = {f["taskId"]: f["flags"] for f in section["flags"]}
    assert flags[late] == ["OVERDUE"] and flags[review] == ["REVIEW_OVERDUE"]
    assert set(status) >= {"journeyTasks", "taskManager"}


# --------------------------------------------------------------------------- time on Verigence + dashboard


def test_activity_only_rises_and_feeds_utilisation(project):
    p = project
    today = tm.ist_today()
    caller = tm.Caller(actor_id=p.people["PC"], role="PC")
    with _db(p) as c:
        hourly = [0] * 24
        hourly[10] = 40
        assert record_activity(c, tenant_id=p.tenant, caller=caller, payload={
            "date": today.isoformat(), "activeSeconds": 3000, "idleSeconds": 600, "backgroundSeconds": 60,
            "hourlyActiveMinutes": hourly, "displayName": "Pc One"})
        # a stale, smaller send changes nothing
        record_activity(c, tenant_id=p.tenant, caller=caller, payload={
            "date": today.isoformat(), "activeSeconds": 100, "idleSeconds": 0, "backgroundSeconds": 0})
        assert not record_activity(c, tenant_id=p.tenant, caller=caller, payload={
            "date": (today - timedelta(days=5)).isoformat(), "activeSeconds": 1, "idleSeconds": 0,
            "backgroundSeconds": 0})
    task_id = _raise(p, "TL", "PC")
    _act(p, "PC", task_id, "COMPLETE_ACTION", comment="done")
    with _db(p) as c:
        report = build_utilisation(c, tenant_id=p.tenant, start_day=today - timedelta(days=6), end_day=today)
    pc = next(x for x in report["people"] if x["actorId"] == p.people["PC"])
    assert pc["name"] == "Pc One"
    assert (pc["totals"]["activeMinutes"], pc["totals"]["idleMinutes"]) == (50, 10)
    assert pc["totals"]["tasksAssigned"] == 1 and pc["totals"]["tasksCompleted"] == 1
    assert pc["totals"]["tasksCompletedOnTime"] == 1 and len(pc["days"]) == 7
    assert pc["days"][-1]["hourlyActiveMinutes"][10] == 40


# --------------------------------------------------------------------------- HTTP


def test_http_routes_and_role_gates(project):
    p = project
    acting = {"subject": p.people["TL"]}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=acting["subject"])
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{p.tenant}"

    context = client.get(f"{base}/task-manager/context").json()
    assert context["canAssignTo"] == ["CRM", "PC", "REVIEWER"] and not context["canCreateSchedules"]
    created = client.post(f"{base}/task-manager/tasks", json={
        "title": "Visit", "description": "Visit the outlet", "assigneeActorIds": [p.people["PC"]],
        "expectedCloseMinutes": 240})
    assert created.status_code == 201
    [task_id] = created.json()["taskIds"]
    assert client.get(f"{base}/reports/employee-utilisation").status_code == 403  # TL
    assert client.get(f"{base}/reports/task-status").status_code == 200
    # a Journey task route does not open a Task Manager task
    assert client.get(f"{base}/tasks/{task_id}").status_code == 404

    acting["subject"] = p.people["PC"]
    assert client.post(f"{base}/task-manager/tasks", json={
        "title": "x", "description": "y", "assigneeActorIds": [p.people["TL"]],
        "expectedCloseMinutes": 60}).status_code == 403
    assert client.post(f"{base}/task-manager/tasks/{task_id}/actions",
                       json={"action": "COMPLETE_ACTION", "comment": "Visited"}).json()["status"] == \
        "AWAITING_REQUESTER_REVIEW"
    assert client.post(f"{base}/activity", json={
        "date": tm.ist_today().isoformat(), "activeSeconds": 60, "idleSeconds": 0,
        "backgroundSeconds": 0}).json() == {"recorded": True}

    acting["subject"] = p.people["EXECUTIVE"]
    assert client.get(f"{base}/reports/employee-utilisation").status_code == 200

    # Superadmin from its own view, one Project at a time.
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id="super-admin-1", bearer_token="t",
        admin_context=SecurityAdminContext(user_id="super-admin-1", is_super_admin=True, admin_scopes=()))
    admin = f"/v1/admin/tenants/{p.tenant}/task-manager"
    listed = client.get(f"{admin}/tasks", params={"view": "all"}).json()
    assert [i["taskId"] for i in listed["items"]] == [task_id]
    raised = client.post(f"{admin}/tasks", json={
        "title": "Audit", "description": "Audit the outlet", "assigneeActorIds": [p.people["EXECUTIVE"]],
        "expectedCloseMinutes": 60})
    assert raised.status_code == 201
    assert client.get(f"{admin}/employee-utilisation").status_code == 200
    assert client.get(f"{admin}/task-status").status_code == 200
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id="x", bearer_token="t",
        admin_context=SecurityAdminContext(user_id="x", is_super_admin=False, admin_scopes=()))
    assert client.get(f"{admin}/tasks").status_code == 403


def test_gate_pass_days_sunday_is_off():
    saturday, sunday, monday = MONDAY - timedelta(days=2), MONDAY - timedelta(days=1), MONDAY
    assert tm.next_gate_pass_day(saturday) == monday and tm.previous_gate_pass_day(monday) == saturday
    assert tm.gate_pass_covers(monday) == [sunday, monday] and tm.gate_pass_covers(saturday) == [saturday]
    assert tm.gate_pass_day_label(monday) == "Sun 1 + Mon 2 Nov"
    assert tm.gate_pass_day_label(date(2026, 10, 26)) == "Sun 25 + Mon 26 Oct"
    assert tm.gate_pass_day_label(saturday) == "Sat 31 Oct"
    # Saturday's extra day would be Sunday: it runs to Monday night instead.
    assert tm.gate_pass_due(saturday) == _at(monday, 23, 59) + timedelta(seconds=59)


def test_monday_gate_pass_task_names_both_days(project):
    p = project
    with _db(p) as c:
        tm.ensure_system_schedules(c, tenant_id=p.tenant, now=_at(MONDAY, 7, 0))
        c.execute(text("UPDATE auditcore.p2_task_schedules SET starts_on = :d WHERE tenant_id = :t"),
                  {"d": MONDAY, "t": p.tenant})
        tm.run_tick(c, tenant_id=p.tenant, now=_at(MONDAY, 7, 5))
        [task] = tm.list_tasks(c, tenant_id=p.tenant, caller=_caller(p, "PC2"), view="mine")["items"]
    assert task["title"].startswith("Gate pass for Sun 1 + Mon 2 Nov: ")
    assert "one photo of the gate pass for both days" in task["description"]
    assert task["reference"]["coversDates"] == ["2026-11-01", "2026-11-02"]
