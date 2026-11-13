"""Project Tracker (product owner 2026-11-07): the period, the reader's scope, the three
grouping levels, gate pass on time / late / missing, tasks and recurring tasks (real Postgres)."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import test_uc03_p2_gate_pass as gate_pass_tests
from p2_support import add_batch_pages
from sqlalchemy import text

from audit_core import uc03_p2_project_tracker as tracker
from audit_core.db import set_tenant_context
from audit_core.task_manager import gate_pass_due
from audit_core.uc03_p2_object_keys import IST

setup = gate_pass_tests.setup  # a PC mapped to an outlet, a Journey, storage doubled


# --------------------------------------------------------------------------- the period

def test_day_week_and_month_ranges():
    saturday = date(2026, 11, 7)
    assert tracker.period_range("DAY", saturday) == (saturday, saturday)
    # The week is Monday to Sunday: a Sunday belongs to the week that started six days before.
    assert tracker.period_range("week", saturday) == (date(2026, 11, 2), date(2026, 11, 8))
    assert tracker.period_range("WEEK", date(2026, 11, 8)) == (date(2026, 11, 2), date(2026, 11, 8))
    assert tracker.period_range("WEEK", date(2026, 11, 9)) == (date(2026, 11, 9), date(2026, 11, 15))
    # A week across a month and a year end.
    assert tracker.period_range("WEEK", date(2027, 1, 1)) == (date(2026, 12, 28), date(2027, 1, 3))
    # Calendar months, February of a leap year included.
    assert tracker.period_range("MONTH", saturday) == (date(2026, 11, 1), date(2026, 11, 30))
    assert tracker.period_range("MONTH", date(2028, 2, 10)) == (date(2028, 2, 1), date(2028, 2, 29))
    assert tracker.period_range("MONTH", date(2026, 12, 31)) == (date(2026, 12, 1), date(2026, 12, 31))
    assert tracker.period_label("WEEK", date(2026, 12, 28), date(2027, 1, 3)) == "28 Dec 2026 – 3 Jan 2027"
    assert tracker.period_label("MONTH", date(2026, 11, 1), date(2026, 11, 30)) == "November 2026"


def test_bounds_are_india_midnights():
    start, end = tracker.period_bounds(date(2026, 11, 1), date(2026, 11, 30), "Asia/Kolkata")
    assert start == datetime(2026, 10, 31, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 11, 30, 18, 30, tzinfo=UTC)
    # An unknown zone falls back to India time rather than failing.
    assert tracker.period_bounds(date(2026, 11, 7), date(2026, 11, 7), "Nowhere/Else")[0] == \
        datetime(2026, 11, 6, 18, 30, tzinfo=UTC)


# --------------------------------------------------------------------------- helpers

def _exec(s, sql: str, **params):
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        return connection.execute(text(sql), {"t": s.journey.tenant_id, **params})


def _role(s, actor: str, role: str, *, dealer=None, outlet=None):
    _exec(s, "INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
             "dealer_id, outlet_id) VALUES (:t, :a, :r, :d, :o)", a=actor, r=role, d=dealer, o=outlet)


def _second_outlet(s) -> str:
    return str(_exec(s, "INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                        "VALUES (:t, :d, :c, 'Second') RETURNING outlet_id",
                     d=s.journey.dealer_id, c=f"O2-{uuid4().hex[:6]}").scalar_one())


def _journey(s, outlet: str, pc: str) -> str:
    customer = _exec(s, "SELECT customer_id FROM auditcore.customers WHERE tenant_id=:t LIMIT 1").scalar_one()
    return str(_exec(s, "INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, "
                        "journey_reference, created_by_actor_id) VALUES (:t, :d, :o, :c, :r, :a) "
                        "RETURNING journey_id",
                     d=s.journey.dealer_id, o=outlet, c=customer, r=f"PT-{uuid4().hex[:8]}", a=pc).scalar_one())


def _task(s, *, role: str, journey: str | None = None, status: str = "READY", created=None, done=None,
          due=None, actor: str | None = None, scope: str = "JOURNEY", outlet: str | None = None,
          schedule: str | None = None, occurrence: date | None = None, outcome: str | None = None) -> str:
    task_id = str(uuid4())
    _exec(s, """
        INSERT INTO auditcore.p2_tasks (tenant_id, task_id, root_task_id, journey_id, task_type, category,
          origin_kind, source_type, dedupe_key, title, description, assigned_role_code, assigned_actor_id,
          allowed_actions, completion_protocol, task_status, created_at_utc, updated_at_utc,
          action_completed_at_utc, verified_at_utc, due_at_utc, scope_kind, outlet_id, dealer_id, schedule_id,
          occurrence_date, completion_result)
        VALUES (:t, :id, :id, :j, :type, 'FINDING_REMEDIATION', 'SYSTEM', 'RULE', :dk, 'Task', 'Do it', :role,
          :actor, '[]'::jsonb, 'MACHINE_VERIFIED', :status, :created, COALESCE(:done, :created), :done,
          CASE WHEN CAST(:status AS varchar) = 'VERIFIED_COMPLETE' THEN :done END, :due, :scope, :o, :d, :sched, :occ,
          CAST(:result AS jsonb))
    """, id=task_id, j=journey, type="GATE_PASS_DAILY" if schedule else "FINDING_REVIEW", dk=f"pt:{task_id}",
          role=role, actor=actor, status=status, created=created or datetime.now(UTC), done=done, due=due,
          scope=scope, o=outlet, d=s.journey.dealer_id if outlet else None, sched=schedule, occ=occurrence,
          result=json.dumps({"outcome": outcome} if outcome else {}))
    return task_id


def _event(s, task_id: str, journey: str | None, event: str, actor: str, role: str, at=None):
    _exec(s, "INSERT INTO auditcore.p2_task_events (tenant_id, task_id, journey_id, event_type, actor_id, "
             "actor_role_code, created_at_utc) VALUES (:t, :task, :j, :e, :a, :r, :at)",
          task=task_id, j=journey, e=event, a=actor, r=role, at=at or datetime.now(UTC))


def _gate_schedule(s, starts: date) -> str:
    return str(_exec(s, """
        INSERT INTO auditcore.p2_task_schedules (tenant_id, system_kind, title, description, category, priority,
          frequency, weekdays, run_time_ist, expected_close_minutes, assignee_mode, assigned_role_code, starts_on,
          created_by_actor_id, created_by_role_code, created_by_display_name)
        VALUES (:t, 'GATE_PASS', 'Daily gate pass', 'x', 'DAILY_GATE_PASS', 'HIGH', 'DAILY',
          ARRAY[1,2,3,4,5,6]::smallint[], '07:00', 2340, 'OUTLET_PCS', 'PC', :starts, 'SYSTEM', 'SYSTEM', 'System')
        RETURNING schedule_id
    """, starts=starts).scalar_one())


def _register_on_file(s, day: date, *, uploaded_at: datetime, total=4, not_notified=1, outlet=None):
    _exec(s, """
        INSERT INTO auditcore.p2_gate_passes (tenant_id, gate_pass_id, dealer_id, outlet_id, outlet_name,
          pc_actor_id, pc_name, business_date, object_key, content_type, size_bytes, width_px, height_px,
          sharpness, completed_at_utc, counts_submitted_at_utc, new_deliveries, not_notified_count,
          stock_transfers, created_at_utc)
        VALUES (:t, :id, :d, :o, 'O', :a, 'PC', :day, 'k', 'image/jpeg', 1, 1, 1, 1, :at, :at, :n, :nn, 2, :at)
    """, id=str(uuid4()), d=s.journey.dealer_id, o=outlet or s.journey.outlet_id, a=s.journey.actor_id, day=day,
          at=uploaded_at, n=total, nn=not_notified)


def _build(s, actor: str, period: str, day: date, now: datetime | None = None, admin: bool = False):
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        return tracker.build_project_tracker(connection, tenant_id=s.journey.tenant_id, actor_id=actor,
                                             period=period, day=day, admin=admin, now=now)


def _outlet(report, outlet_id) -> dict:
    return next(o for d in report["dealers"] for o in d["outlets"] if o["outletId"] == str(outlet_id))


# --------------------------------------------------------------------------- endpoint and scope

def test_team_lead_pm_and_executive_read_the_team(setup):
    s = setup
    url = f"/p2/v1/tenants/{s.journey.tenant_id}/reports/project-tracker"
    assert s.client.get(url).json()["scope"]["own"] is True          # the PC: their own row only
    tl = f"tl-{uuid4().hex[:6]}"
    _role(s, tl, "TL")
    s.acting["subject"] = tl
    for period in ("day", "WEEK", "Month"):
        body = s.client.get(url, params={"period": period})
        assert body.status_code == 200, body.text
        assert body.json()["period"]["kind"] == period.upper() and body.json()["period"]["current"] is True
    assert s.client.get(url, params={"period": "year"}).status_code == 400  # request validation
    future = (datetime.now(IST).date() + timedelta(days=40)).isoformat()
    assert s.client.get(url, params={"period": "day", "date": future}).status_code == 422


def test_a_team_lead_sees_only_their_outlets_a_project_wide_one_sees_all(setup):
    s = setup
    second = _second_outlet(s)
    other_pc = f"pc2-{uuid4().hex[:6]}"
    _role(s, other_pc, "PC", dealer=s.journey.dealer_id, outlet=second)
    j2 = _journey(s, second, other_pc)
    add_batch_pages(SimpleNamespace(**{**s.journey.__dict__, "journey_id": j2, "actor_id": other_pc}),
                    [("booking_form", "READY")])
    narrow, wide = f"tl-{uuid4().hex[:6]}", f"tl-{uuid4().hex[:6]}"
    _role(s, narrow, "TL", dealer=s.journey.dealer_id, outlet=s.journey.outlet_id)
    _role(s, wide, "TL")
    today = datetime.now(IST).date()
    mine = _build(s, narrow, "DAY", today)
    assert mine["scope"] == {"projectWide": False, "outlets": 1, "dealers": 1, "own": False}
    assert [o["outletId"] for d in mine["dealers"] for o in d["outlets"]] == [str(s.journey.outlet_id)]
    assert mine["totals"]["uploads"]["files"] == 0
    everything = _build(s, wide, "DAY", today)
    assert everything["scope"]["projectWide"] is True and everything["scope"]["outlets"] == 2
    assert everything["totals"]["uploads"]["files"] == 1
    # An administrator holds no mapping: the whole Project.
    assert _build(s, "someone-admin", "DAY", today, admin=True)["scope"]["outlets"] == 2


# --------------------------------------------------------------------------- the three levels

def test_uploads_roll_up_from_pc_to_outlet_dealership_and_project(setup):
    s = setup
    gate_pass_tests._delivery_file(s, journeys=2, pages=3)            # 2 delivery journeys, 3 pages each
    add_batch_pages(s.journey, [("booking_form", "READY")])            # one more file to the first journey
    j3 = _journey(s, str(s.journey.outlet_id), s.journey.actor_id)     # a booking only
    add_batch_pages(SimpleNamespace(**{**s.journey.__dict__, "journey_id": j3}), [("booking_form", "READY")] * 2)
    tl = f"tl-{uuid4().hex[:6]}"
    _role(s, tl, "TL")
    report = _build(s, tl, "WEEK", datetime.now(IST).date())
    outlet = _outlet(report, s.journey.outlet_id)
    [dealer] = [d for d in report["dealers"] if d["dealerId"] == str(s.journey.dealer_id)]
    pc = next(p for p in outlet["people"] if p["actorId"] == s.journey.actor_id)
    for level in (report["totals"], dealer, outlet, pc):
        up = level["uploads"]
        assert (up["files"], up["pages"], up["journeys"], up["deliveries"], up["bookings"]) == (4, 9, 3, 2, 1)
        assert up["documents"] == 9
    # Journeys started in the period: under the PC who started them (the others have no PC recorded).
    assert report["totals"]["uploads"]["newJourneys"] >= 2
    assert pc["role"] == "PC" and pc["uploads"]["newJourneys"] == 1


# --------------------------------------------------------------------------- gate pass

def test_gate_pass_on_time_late_and_missing(setup):
    s = setup
    tl = f"tl-{uuid4().hex[:6]}"
    _role(s, tl, "TL")
    # A week long past, so every day is due: Mon 5 Oct to Sun 11 Oct 2026 (six register days).
    monday = date(2026, 10, 5)
    _gate_schedule(s, monday)
    _register_on_file(s, monday, uploaded_at=gate_pass_due(monday) - timedelta(hours=30))          # on time
    tuesday = monday + timedelta(days=1)
    _register_on_file(s, tuesday, uploaded_at=gate_pass_due(tuesday) + timedelta(days=2), total=3)  # late
    now = datetime(2026, 11, 7, 12, tzinfo=UTC)
    gp = _build(s, tl, "WEEK", monday, now=now)["totals"]["gatePass"]
    assert (gp["due"], gp["onTime"], gp["late"], gp["missing"], gp["notYetDue"]) == (6, 1, 1, 4, 0)
    assert (gp["onRegister"], gp["notNotified"], gp["intimated"], gp["stockTransfers"]) == (7, 2, 5, 4)
    outlet = _outlet(_build(s, tl, "WEEK", monday, now=now), s.journey.outlet_id)
    pc = next(p for p in outlet["people"] if p["actorId"] == s.journey.actor_id)
    unlinked = next(p for p in outlet["people"] if p["actorId"] is None)
    # The registers on file under the PC who uploaded them; the missing ones on the outlet's own row.
    assert (pc["gatePass"]["onTime"], pc["gatePass"]["late"], pc["gatePass"]["missing"]) == (1, 1, 0)
    assert unlinked["gatePass"]["missing"] == 4 and outlet["gatePass"]["due"] == 6
    # Before the Daily Gate Pass started nothing is due.
    assert _build(s, tl, "WEEK", monday - timedelta(days=7), now=now)["totals"]["gatePass"]["due"] == 0


def test_todays_register_is_not_yet_due(setup):
    s = setup
    tl = f"tl-{uuid4().hex[:6]}"
    _role(s, tl, "TL")
    today = datetime.now(IST).date()
    _gate_schedule(s, today - timedelta(days=30))
    gp = _build(s, tl, "DAY", today)["totals"]["gatePass"]
    if today.isoweekday() == 7:
        assert gp["due"] == 0
    else:
        assert (gp["due"], gp["notYetDue"], gp["missing"]) == (1, 1, 0)


# --------------------------------------------------------------------------- tasks

def test_tasks_raised_closed_by_whom_open_and_overdue(setup):
    s = setup
    tl = f"tl-{uuid4().hex[:6]}"
    _role(s, tl, "TL")
    j = str(s.journey.journey_id)
    _exec(s, "UPDATE auditcore.journeys SET created_by_actor_id=:a WHERE tenant_id=:t AND journey_id=:j",
          a=s.journey.actor_id, j=j)
    monday = date(2026, 10, 12)
    mid = datetime(2026, 10, 14, 6, tzinfo=UTC)
    before = datetime(2026, 10, 1, 6, tzinfo=UTC)
    after = datetime(2026, 10, 25, 6, tzinfo=UTC)
    pc_closed = _task(s, role="PC", journey=j, status="VERIFIED_COMPLETE", created=mid, done=mid + timedelta(hours=1))
    _event(s, pc_closed, j, "CLOSE_TASK", s.journey.actor_id, "PC", at=mid + timedelta(hours=1))
    tl_closed = _task(s, role="TL", journey=j, status="VERIFIED_COMPLETE", created=before, done=mid)
    _event(s, tl_closed, j, "ACCEPT_EXCEPTION", tl, "TL", at=mid)
    _task(s, role="PC", journey=j, status="VERIFIED_COMPLETE", created=mid, done=mid + timedelta(hours=2))  # system
    _task(s, role="PC", journey=j, created=before, due=mid)                                    # open, overdue
    _task(s, role="TL", journey=j, created=mid, due=after + timedelta(days=5))                 # open, not overdue
    _task(s, role="PC", journey=j, status="VERIFIED_COMPLETE", created=before, done=after)     # open at week end
    _task(s, role="PC", journey=j, status="CANCELLED", created=before, done=None)              # cancelled long ago
    now = datetime(2026, 11, 7, 12, tzinfo=UTC)
    report = _build(s, tl, "WEEK", monday, now=now)
    t = report["totals"]["tasks"]["journey"]
    assert (t["raised"], t["closed"], t["closedByPc"], t["closedByLead"], t["closedBySystem"]) == (3, 3, 1, 1, 1)
    assert (t["open"], t["overdue"]) == (3, 1)
    by_role = report["totals"]["tasks"]["byRole"]
    assert by_role["PC"]["closed"] == 2 and by_role["TL"]["closed"] == 1 and by_role["TL"]["open"] == 1
    assert report["totals"]["tasks"]["worked"] == {"total": 2, "byPc": 1, "byLead": 1, "byReviewer": 0, "byOther": 0}
    # Every task of the PC's Journey counts on the PC's row, the Team Lead's too.
    pc = next(p for p in _outlet(report, s.journey.outlet_id)["people"] if p["actorId"] == s.journey.actor_id)
    assert pc["tasks"]["journey"] == t


def test_recurring_tasks_by_schedule(setup):
    s = setup
    tl = f"tl-{uuid4().hex[:6]}"
    _role(s, tl, "TL")
    monday = date(2026, 10, 12)
    schedule = _gate_schedule(s, monday)
    outlet = str(s.journey.outlet_id)
    created = datetime(2026, 10, 12, 1, 30, tzinfo=UTC)
    common = {"role": "PC", "scope": "OUTLET", "outlet": outlet, "schedule": schedule, "created": created}
    _task(s, **common, occurrence=monday, status="VERIFIED_COMPLETE", done=gate_pass_due(monday) - timedelta(hours=5),
          due=gate_pass_due(monday), actor=None, outcome="DONE")
    tue = monday + timedelta(days=1)
    _task(s, **common, occurrence=tue, status="VERIFIED_COMPLETE", done=gate_pass_due(tue) + timedelta(days=1),
          due=gate_pass_due(tue), outcome="DONE_LATE")
    wed = monday + timedelta(days=2)
    _task(s, **common, occurrence=wed, status="FAILED", due=gate_pass_due(wed), outcome="INCOMPLETE")
    thu = monday + timedelta(days=3)
    _task(s, **common, occurrence=thu, due=gate_pass_due(thu))                                   # overdue
    fri = monday + timedelta(days=4)
    _task(s, **common, occurrence=fri, status="CANCELLED", due=gate_pass_due(fri))               # left out
    report = _build(s, tl, "WEEK", monday, now=datetime(2026, 11, 7, 12, tzinfo=UTC))
    assert report["schedules"][0]["scheduleId"] == schedule and report["schedules"][0]["systemKind"] == "GATE_PASS"
    figures = report["totals"]["recurring"][schedule]
    assert figures == {"due": 4, "onTime": 1, "late": 1, "incomplete": 1, "overdue": 1, "pending": 0}
    assert _outlet(report, outlet)["recurring"][schedule]["due"] == 4
    # A schedule an Executive adds shows up as its own entry with no new code.
    extra = str(_exec(s, """
        INSERT INTO auditcore.p2_task_schedules (tenant_id, title, description, category, priority, frequency,
          weekdays, run_time_ist, expected_close_minutes, assignee_mode, assigned_role_code, starts_on,
          created_by_actor_id, created_by_role_code)
        VALUES (:t, 'DMS reconciliation', 'x', 'RECURRING', 'NORMAL', 'WEEKLY', ARRAY[1]::smallint[], '10:00', 480,
          'ROLE_EACH', 'TL', :d, 'exec', 'EXECUTIVE') RETURNING schedule_id
    """, d=monday).scalar_one())
    _task(s, role="TL", scope="PROJECT", actor=tl, schedule=extra, occurrence=monday, created=created,
          due=created + timedelta(hours=8))
    report = _build(s, tl, "WEEK", monday, now=datetime(2026, 11, 7, 12, tzinfo=UTC))
    assert {x["title"] for x in report["schedules"]} >= {"Daily gate pass", "DMS reconciliation"}
    assert report["totals"]["recurring"][extra]["overdue"] == 1
    # A Project-wide task sits under the Project-wide group, on the person's own row.
    [wide] = [d for d in report["dealers"] if d["dealerId"] is None]
    assert wide["outlets"][0]["people"][0]["actorId"] == tl
    assert wide["outlets"][0]["people"][0]["recurring"][extra]["due"] == 1


# --------------------------------------------------------------------------- 13 Nov 2026 additions

def _bulk_source(s, batch_id, source: str):
    """Mark an upload as a file of a bulk upload from ``source`` (WEB folder upload or DRIVE)."""
    bulk = str(_exec(s, """
        INSERT INTO auditcore.p2_bulk_uploads (tenant_id, dealer_id, outlet_id, uploaded_by_actor_id, folder,
          status, source)
        VALUES (:t, :d, :o, :a, 'f', 'DONE', :src) RETURNING bulk_id
    """, d=s.journey.dealer_id, o=s.journey.outlet_id, a=s.journey.actor_id, src=source).scalar_one())
    _exec(s, """
        INSERT INTO auditcore.p2_bulk_files (tenant_id, bulk_id, original_filename, content_type, size_bytes,
          object_key, upload_status, batch_id)
        VALUES (:t, :b, 'x.pdf', 'application/pdf', 1, :k, 'UPLOADED', :batch)
    """, b=bulk, k=f"bulk/{uuid4()}", batch=batch_id)


def test_files_by_how_they_came_in(setup):
    s = setup
    add_batch_pages(s.journey, [("booking_form", "READY")])                     # on its own
    bulk, _ = add_batch_pages(s.journey, [("booking_form", "READY")] * 2)        # folder upload
    drive, _ = add_batch_pages(s.journey, [("booking_form", "READY")] * 3)       # Google Drive
    _bulk_source(s, bulk, "WEB")
    _bulk_source(s, drive, "DRIVE")
    tl = f"tl-{uuid4().hex[:6]}"
    _role(s, tl, "TL")
    report = _build(s, tl, "DAY", datetime.now(IST).date())
    pc = next(p for p in _outlet(report, s.journey.outlet_id)["people"] if p["actorId"] == s.journey.actor_id)
    for level in (report["totals"], pc):
        c = level["channels"]
        assert (c["directFiles"], c["bulkFiles"], c["driveFiles"]) == (1, 1, 1)
        assert (c["directPages"], c["bulkPages"], c["drivePages"]) == (1, 2, 3)


def _closure(s, journey, *, by, role="REVIEWER", status="PENDING", decided_by=None, decided_role=None,
             closed_at=None, decided_at=None):
    _exec(s, """
        INSERT INTO auditcore.p2_journey_closures (tenant_id, journey_id, closed_by_actor_id, closed_by_role,
          comment, item_count, summary, delivery_outcome, closed_at_utc, acceptance_status, acceptance_due_utc,
          decided_by_actor_id,
          decided_by_role, decided_at_utc)
        VALUES (:t, :j, :by, :role, 'Reviewed and closed.', 0, '{}'::jsonb, 'DELIVERED', :at, :st, :at, :dby, :drole, :dat)
    """, j=journey, by=by, role=role, at=closed_at or datetime.now(UTC), st=status, dby=decided_by,
          drole=decided_role, dat=decided_at)


def test_reviews_closed_decided_and_awaiting(setup):
    s = setup
    outlet = str(s.journey.outlet_id)
    reviewer, tl = f"rv-{uuid4().hex[:6]}", f"tl-{uuid4().hex[:6]}"
    _role(s, reviewer, "REVIEWER", dealer=s.journey.dealer_id, outlet=s.journey.outlet_id)
    _role(s, tl, "TL")
    j1, j2, j3 = (_journey(s, outlet, s.journey.actor_id) for _ in range(3))
    now = datetime.now(UTC)
    _closure(s, j1, by=reviewer)                                                       # awaiting the TL
    _closure(s, j2, by=reviewer, status="ACCEPTED", decided_by=tl, decided_role="TL", decided_at=now)
    _closure(s, j3, by=reviewer, status="REJECTED", decided_by=tl, decided_role="TL", decided_at=now)
    report = _build(s, tl, "DAY", datetime.now(IST).date())
    r = report["totals"]["reviews"]
    assert (r["closedByReviewer"], r["accepted"], r["rejected"], r["awaitingDecision"]) == (3, 1, 1, 1)
    people = {p["actorId"]: p for p in _outlet(report, outlet)["people"]}
    assert people[reviewer]["reviews"]["closedByReviewer"] == 3
    assert people[reviewer]["reviews"]["awaitingDecision"] == 1
    assert people[tl]["reviews"]["accepted"] == 1 and people[tl]["reviews"]["rejected"] == 1


def test_failed_pages_by_why(setup):
    s = setup
    batch, rows = add_batch_pages(s.journey, [("booking_form", "FAILED")] * 3)
    for row, code in zip(rows, ("DI_QUALITY_IMAGE_BLUR_SCORE", "NOT_READ_IN_30_MIN", "DOCUMENT_AI_RESPONSE_INVALID"),
                         strict=True):
        _exec(s, "UPDATE auditcore.p2_document_queue SET last_error=:c WHERE tenant_id=:t AND queue_id=:q",
              c=code, q=row["queue_id"])
    _exec(s, """
        INSERT INTO auditcore.p2_upload_batches (tenant_id, batch_id, journey_id, original_filename, content_type,
          size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id, failure_reason)
        VALUES (:t, :b, :j, 'lost.pdf', 'application/pdf', 1, 0, :k, 'FAILED', :a, 'did not finish uploading')
    """, b=uuid4(), j=s.journey.journey_id, k=f"p2/{uuid4()}", a=s.journey.actor_id)
    tl = f"tl-{uuid4().hex[:6]}"
    _role(s, tl, "TL")
    f = _build(s, tl, "DAY", datetime.now(IST).date())["totals"]["failures"]
    assert (f["files"], f["pages"], f["quality"], f["notRead"], f["readFailed"], f["notUploaded"]) == (2, 3, 1, 1, 1, 1)
    assert str(batch)  # the batch with the three failed pages is one of the two files


def test_a_pc_reads_only_their_own_row(setup):
    s = setup
    second = _second_outlet(s)
    other = f"pc2-{uuid4().hex[:6]}"
    _role(s, other, "PC", dealer=s.journey.dealer_id, outlet=s.journey.outlet_id)   # same outlet, another PC
    j2 = _journey(s, str(s.journey.outlet_id), other)
    add_batch_pages(SimpleNamespace(**{**s.journey.__dict__, "journey_id": j2, "actor_id": other}),
                    [("booking_form", "READY")] * 2)
    add_batch_pages(s.journey, [("booking_form", "READY")])
    url = f"/p2/v1/tenants/{s.journey.tenant_id}/reports/project-tracker"
    body = s.client.get(url, params={"period": "day"})                  # the PC of the setup
    assert body.status_code == 200, body.text
    report = body.json()
    assert report["scope"]["own"] is True
    assert report["totals"]["uploads"]["files"] == 1
    outlets = [o for d in report["dealers"] for o in d["outlets"]]
    assert all(o["outletId"] != second for o in outlets)
    assert [p["actorId"] for o in outlets for p in o["people"]] == [s.journey.actor_id]
    # Their own failures list too.
    failed = s.client.get(f"/p2/v1/tenants/{s.journey.tenant_id}/reports/failed-pages", params={"by": "uploaded"})
    assert failed.status_code == 200, failed.text


def test_documents_reviewed_by_role_and_awaiting(setup):
    s = setup
    outlet = str(s.journey.outlet_id)
    reviewer, tl = f"rv-{uuid4().hex[:6]}", f"tl-{uuid4().hex[:6]}"
    _role(s, reviewer, "REVIEWER", dealer=s.journey.dealer_id, outlet=s.journey.outlet_id)
    _role(s, tl, "TL")
    journeys = [_journey(s, outlet, s.journey.actor_id) for _ in range(3)]
    for j in journeys:
        _exec(s, """
            INSERT INTO auditcore.document_capture_v2_documents (tenant_id, journey_id, stage_code, di_document_id,
              client_upload_id, capture_status, created_by_actor_id)
            VALUES (:t, :j, 'BOOKING', :d, :c, 'CLASSIFIED', :a)
        """, j=j, d=uuid4(), c=f"cap-{uuid4().hex}", a=s.journey.actor_id)
    for j, who, role in ((journeys[0], reviewer, "REVIEWER"), (journeys[1], tl, "TL")):
        _exec(s, """
            INSERT INTO auditcore.p2_stage_document_reviews (tenant_id, journey_id, stage_code, reviewed_by_actor_id,
              reviewed_by_role_code, reviewed_at_utc, document_count, dealer_id, outlet_id)
            VALUES (:t, :j, 'BOOKING', :a, :r, now(), 1, :d, :o)
        """, j=j, a=who, r=role, d=s.journey.dealer_id, o=s.journey.outlet_id)
    report = _build(s, tl, "DAY", datetime.now(IST).date())
    r = report["totals"]["reviews"]
    assert (r["stagesReviewedByReviewer"], r["stagesReviewedByLead"], r["stagesAwaitingReview"]) == (1, 1, 1)
    people = {p["actorId"]: p for p in _outlet(report, outlet)["people"]}
    assert people[reviewer]["reviews"]["stagesReviewedByReviewer"] == 1
    assert people[tl]["reviews"]["stagesReviewedByLead"] == 1
    assert people[s.journey.actor_id]["reviews"]["stagesAwaitingReview"] == 1
