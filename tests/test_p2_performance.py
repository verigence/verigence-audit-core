"""Performance view (product owner 2026-11-13): each person's day against their usual and their peers."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import test_p2_project_tracker as tt
import test_uc03_p2_gate_pass as gate_pass_tests
from p2_support import add_batch_pages

from audit_core import uc03_p2_performance as perf
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_object_keys import IST

setup = gate_pass_tests.setup


def _build(s, actor: str, *, own: bool = False, admin: bool = False, day: date | None = None,
           section: str | None = None):
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        return perf.build_performance(connection, tenant_id=s.journey.tenant_id, actor_id=actor,
                                      day=day or datetime.now(IST).date(), own=own, admin=admin, use_cache=False,
                                      section=section)


def _uploads(s, actor: str, journey: str, files: int, at: datetime | None = None):
    for _ in range(files):
        batch, _rows = add_batch_pages(
            SimpleNamespace(**{**s.journey.__dict__, "journey_id": journey, "actor_id": actor}),
            [("booking_form", "READY")])
        if at is not None:
            tt._exec(s, "UPDATE auditcore.p2_upload_batches SET created_at_utc=:at WHERE tenant_id=:t AND batch_id=:b",
                     at=at, b=batch)


def test_standing_against_the_peer_median():
    assert perf.standing(10, None) is None
    assert perf.standing(10, 10) == "with" and perf.standing(10.5, 10) == "with"
    assert perf.standing(14, 10) == "above" and perf.standing(6, 10) == "below"
    # Fewer rejected pages is better.
    assert perf.standing(1, 6, lower_is_better=True) == "above"
    assert perf.standing(9, 6, lower_is_better=True) == "below"


def test_each_person_against_their_usual_and_their_peers(setup):
    s = setup
    outlet = str(s.journey.outlet_id)
    me = s.journey.actor_id
    others = [f"pc-{uuid4().hex[:6]}" for _ in range(2)]
    for other in others:
        tt._role(s, other, "PC", dealer=s.journey.dealer_id, outlet=s.journey.outlet_id)
    journeys = {a: tt._journey(s, outlet, a) for a in (me, *others)}
    _uploads(s, me, journeys[me], 6)
    _uploads(s, others[0], journeys[others[0]], 2)
    _uploads(s, others[1], journeys[others[1]], 3)
    # Two days ago I uploaded two files: my usual.
    _uploads(s, me, journeys[me], 2, at=datetime.now(UTC) - timedelta(days=2))
    tl = f"tl-{uuid4().hex[:6]}"
    tt._role(s, tl, "TL")
    report = _build(s, tl)
    assert report["own"] is False and len(report["days"]) == perf.DEFAULT_DAYS
    people = {p["actorId"]: p for p in report["people"]}
    assert {me, *others, tl} <= set(people)
    mine = people[me]
    assert mine["today"]["filesUploaded"] == 6 and mine["series"]["filesUploaded"][-1] == 6
    assert mine["series"]["filesUploaded"][-3] == 2
    assert mine["standing"]["filesUploaded"] == "above"            # the median of 6, 2 and 3 is 3
    assert people[others[0]]["standing"]["filesUploaded"] == "below"
    assert people[others[1]]["standing"]["filesUploaded"] == "with"
    assert report["peers"]["PC"]["median"]["filesUploaded"] == 3 and report["peers"]["PC"]["activeToday"] == 3
    assert mine["usual"]["filesUploaded"] is not None and mine["usual"]["filesUploaded"] < 6
    # Someone who did nothing today is not ranked against the others.
    assert people[tl]["activeToday"] is False and people[tl]["standing"]["tasksClosed"] is None
    # Cash register is named, not set up yet.
    assert report["plannedDuties"] == [{"title": "Cash register", "frequency": "DAILY"}]


def test_a_pc_reads_their_own_row_and_an_unnamed_peer_median(setup):
    s = setup
    other = f"pc-{uuid4().hex[:6]}"
    tt._role(s, other, "PC", dealer=s.journey.dealer_id, outlet=s.journey.outlet_id)
    _uploads(s, other, tt._journey(s, str(s.journey.outlet_id), other), 4)
    add_batch_pages(s.journey, [("booking_form", "READY")])
    perf.clear_cache()
    body = s.client.get(f"/p2/v1/tenants/{s.journey.tenant_id}/reports/performance")
    assert body.status_code == 200, body.text
    report = body.json()
    assert report["own"] is True
    assert [p["actorId"] for p in report["people"]] == [s.journey.actor_id]
    assert other not in body.text                                   # nobody else is named
    assert report["peers"]["PC"]["median"]["filesUploaded"] == 2.5   # 4 and 1
    assert report["people"][0]["standing"]["filesUploaded"] == "below"


def test_duties_come_from_the_recurring_schedules(setup):
    s = setup
    today = datetime.now(IST).date()
    schedule = tt._gate_schedule(s, today - timedelta(days=10))
    now = datetime.now(UTC)
    tt._task(s, role="PC", actor=s.journey.actor_id, scope="OUTLET", outlet=str(s.journey.outlet_id),
             schedule=schedule, occurrence=today, status="VERIFIED_COMPLETE", created=now - timedelta(hours=3),
             done=now - timedelta(hours=1), due=now + timedelta(hours=5))
    tt._task(s, role="PC", actor=s.journey.actor_id, scope="OUTLET", outlet=str(s.journey.outlet_id),
             schedule=schedule, occurrence=today - timedelta(days=1), status="READY",
             created=now - timedelta(days=1, hours=3), due=now - timedelta(hours=2))
    tl = f"tl-{uuid4().hex[:6]}"
    tt._role(s, tl, "TL")
    report = _build(s, tl)
    mine = next(p for p in report["people"] if p["actorId"] == s.journey.actor_id)
    assert mine["dutiesToday"][schedule] == {"due": 1, "onTime": 1, "late": 0, "notDone": 0, "pending": 0}
    assert mine["dutiesWindow"][schedule]["due"] == 2 and mine["dutiesWindow"][schedule]["notDone"] == 1
    assert [x["scheduleId"] for x in report["schedules"]] == [schedule]


def test_a_team_lead_reads_only_the_people_of_their_outlets(setup):
    s = setup
    second = tt._second_outlet(s)
    elsewhere = f"pc-{uuid4().hex[:6]}"
    tt._role(s, elsewhere, "PC", dealer=s.journey.dealer_id, outlet=second)
    # A Team Lead of another dealership's outlet only.
    dealer2 = str(tt._exec(s, "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                              "VALUES (:t, :c, 'Other') RETURNING dealer_id", c=f"D2-{uuid4().hex[:6]}").scalar_one())
    outlet2 = str(tt._exec(s, "INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                              "VALUES (:t, :d, :c, 'Far') RETURNING outlet_id",
                           d=dealer2, c=f"O-{uuid4().hex[:6]}").scalar_one())
    narrow = f"tl-{uuid4().hex[:6]}"
    tt._role(s, narrow, "TL", dealer=dealer2, outlet=outlet2)
    ids = {p["actorId"] for p in _build(s, narrow)["people"]}
    assert narrow in ids and elsewhere not in ids and s.journey.actor_id not in ids


# --------------------------------------------------------------------------- SuperAdmin settings

def _clear_settings(s):
    with s.journey.engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM platform_ops.performance_settings")


def test_a_super_admin_chooses_what_each_role_sees(setup):
    from fastapi.testclient import TestClient

    from audit_core.dependencies import HumanAdminRequest, require_super_admin_request
    from audit_core.main import app
    from audit_core.security_integration import SecurityAdminContext

    s = setup
    _clear_settings(s)
    add_batch_pages(s.journey, [("booking_form", "READY")])
    tl, pm = f"tl-{uuid4().hex[:6]}", f"pm-{uuid4().hex[:6]}"
    tt._role(s, tl, "TL")
    tt._role(s, pm, "PM")
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id="sa-1", bearer_token="t",
        admin_context=SecurityAdminContext(user_id="sa-1", is_super_admin=True, admin_scopes=()))
    try:
        admin = TestClient(app, raise_server_exceptions=False)
        page = admin.get("/v1/admin/performance-settings")
        assert page.status_code == 200, page.text
        assert page.json()["roles"] == ["PC", "REVIEWER", "TL", "PM", "EXECUTIVE"]
        assert all(all(item["shown"].values()) for item in page.json()["items"])
        assert {"pending", "flow", "trend", "efficiency", "duties", "activeMinutes"} <= {
            i["key"] for i in page.json()["items"]}
        assert admin.put("/v1/admin/performance-settings", json={"shown": {"TL:nonsense": False}}).status_code == 422
        changed = admin.put("/v1/admin/performance-settings",
                            json={"shown": {"TL:activeMinutes": False, "TL:efficiency": False, "TL:pending": False}})
        assert changed.status_code == 200, changed.text
        by_key = {i["key"]: i["shown"] for i in changed.json()["items"]}
        assert by_key["efficiency"]["TL"] is False and by_key["efficiency"]["PM"] is True
        # The Team Lead no longer gets them; the PM still does.
        for_tl, for_pm = _build(s, tl), _build(s, pm)
        assert "activeMinutes" not in [m["key"] for m in for_tl["measures"]]
        assert "efficiency" not in for_tl and "pending" not in for_tl
        assert [x["key"] for x in for_tl["sections"]] == ["flow", "uploads", "quality", "tasks", "trend", "duties"]
        assert "activeMinutes" in [m["key"] for m in for_pm["measures"]]
        assert "efficiency" in for_pm and "pending" in for_pm
        mine = next(p for p in for_tl["people"] if p["actorId"] == s.journey.actor_id)
        assert "activeMinutes" not in mine["today"]
    finally:
        app.dependency_overrides.pop(require_super_admin_request, None)
        _clear_settings(s)


def test_journeys_closed_by_a_reviewer_count_on_the_reviewer(setup):
    s = setup
    reviewer, tl = f"rv-{uuid4().hex[:6]}", f"tl-{uuid4().hex[:6]}"
    tt._role(s, reviewer, "REVIEWER", dealer=s.journey.dealer_id, outlet=s.journey.outlet_id)
    tt._role(s, tl, "TL")
    for _ in range(2):
        tt._closure(s, tt._journey(s, str(s.journey.outlet_id), s.journey.actor_id), by=reviewer)
    report = _build(s, tl)
    assert "journeysClosed" in [m["key"] for m in report["measures"]]
    people = {p["actorId"]: p for p in report["people"]}
    assert people[reviewer]["today"]["journeysClosed"] == 2 and people[reviewer]["today"]["reviews"] == 0


def test_outlet_and_dealership_views_and_who_reads_whom(setup):
    s = setup
    outlet = str(s.journey.outlet_id)
    second = tt._second_outlet(s)
    other = f"pc-{uuid4().hex[:6]}"
    tt._role(s, other, "PC", dealer=s.journey.dealer_id, outlet=second)
    _uploads(s, s.journey.actor_id, tt._journey(s, outlet, s.journey.actor_id), 5)
    _uploads(s, other, tt._journey(s, second, other), 1)
    reviewer, tl, tl2, pm = (f"{r}-{uuid4().hex[:6]}" for r in ("rv", "tl", "tl", "pm"))
    tt._role(s, reviewer, "REVIEWER", dealer=s.journey.dealer_id, outlet=s.journey.outlet_id)
    tt._role(s, tl, "TL")
    tt._role(s, tl2, "TL")
    tt._role(s, pm, "PM")
    report = _build(s, tl)
    by_id = {o["outletId"]: o for o in report["outlets"]}
    assert by_id[outlet]["today"]["filesUploaded"] == 5 and by_id[second]["today"]["filesUploaded"] == 1
    assert by_id[outlet]["standing"]["filesUploaded"] == "above" and by_id[second]["standing"]["filesUploaded"] == "below"
    [dealer] = [d for d in report["dealers"] if d["dealerId"] == str(s.journey.dealer_id)]
    assert dealer["today"]["filesUploaded"] == 6 and dealer["outlets"] == 2
    assert "activeMinutes" not in [m["key"] for m in report["unitMeasures"]]
    # A Team Lead reads PCs and Reviewers, not the other Team Leads; a PM reads the Team Leads too.
    seen = {p["actorId"] for p in report["people"]}
    assert {s.journey.actor_id, other, reviewer, tl} <= seen and tl2 not in seen and pm not in seen
    seen_by_pm = {p["actorId"] for p in _build(s, pm)["people"]}
    assert {tl, tl2, reviewer} <= seen_by_pm
    mine = next(p for p in report["people"] if p["actorId"] == s.journey.actor_id)
    assert mine["outletIds"] == [outlet]
    # A PC's own view carries no outlets or dealerships.
    own = _build(s, s.journey.actor_id, own=True)
    assert own["outlets"] == [] and own["dealers"] == []


def test_pending_work_efficiency_delinquencies_and_bookings_deliveries(setup):
    s = setup
    outlet = str(s.journey.outlet_id)
    me = s.journey.actor_id
    today = datetime.now(IST).date()
    now = datetime.now(UTC)
    # Two delivery files of mine, uploaded today and five days ago; and a booking today.
    gate_pass_tests._delivery_file(s, journeys=2, pages=2)
    _uploads(s, me, tt._journey(s, outlet, me), 1)
    old = tt._journey(s, outlet, me)
    batch, _rows = add_batch_pages(SimpleNamespace(**{**s.journey.__dict__, "journey_id": old}), [("gate_pass", "READY")])
    tt._exec(s, "UPDATE auditcore.p2_document_queue SET business_stage='DELIVERY', created_at_utc=:at "
                "WHERE tenant_id=:t AND batch_id=:b", at=now - timedelta(days=5), b=batch)
    tt._exec(s, "UPDATE auditcore.p2_upload_batches SET created_at_utc=:at WHERE tenant_id=:t AND batch_id=:b",
             at=now - timedelta(days=5), b=batch)
    # Tasks: one open and overdue, one closed in time.
    tt._task(s, role="PC", journey=old, actor=me, created=now - timedelta(days=2), due=now - timedelta(days=1))
    done = tt._task(s, role="PC", journey=old, actor=me, status="VERIFIED_COMPLETE", created=now - timedelta(hours=5),
                    done=now - timedelta(hours=1), due=now + timedelta(hours=3))
    tt._exec(s, "UPDATE auditcore.p2_tasks SET completed_by_actor_id=:a WHERE tenant_id=:t AND task_id=:id", a=me, id=done)
    # A duty missed yesterday.
    schedule = tt._gate_schedule(s, today - timedelta(days=10))
    tt._task(s, role="PC", actor=me, scope="OUTLET", outlet=outlet, schedule=schedule,
             occurrence=today - timedelta(days=1), status="READY", created=now - timedelta(days=1, hours=3),
             due=now - timedelta(hours=2))
    tl = f"tl-{uuid4().hex[:6]}"
    tt._role(s, tl, "TL")
    report = _build(s, tl)
    assert [x["key"] for x in report["sections"]] == ["pending", "flow", "uploads", "quality", "tasks", "trend",
                                                      "efficiency", "duties"]

    pending = next(p for p in report["pending"]["people"] if p["actorId"] == me)["counts"]
    assert pending["deliveryFilesOpen"][2] == 1                       # the one uploaded five days ago
    assert sum(pending["tasksOpen"]) >= 1 and sum(pending["tasksOverdue"]) >= 1
    at_outlet = next(o for o in report["pending"]["outlets"] if o["outletId"] == outlet)["counts"]
    assert at_outlet["deliveryFilesOpen"][2] == 1

    eff = next(e for e in report["efficiency"]["people"] if e["actorId"] == me)
    assert eff["deliveryStarted"] >= 1 and eff["tasksWithDue"] == 1 and eff["tasksInTimeShare"] == 100
    assert eff["firstTimeRight"] == 100 and eff["journeysPerDay"] is not None

    line = next(x for x in report["delinquency"]["lines"] if x["actorId"] == me)
    assert line["notDone"] == 1 and line["missedDates"] == [(today - timedelta(days=1)).isoformat()]

    flow = next(o for o in report["flow"]["outlets"] if o["outletId"] == outlet)["days"]
    assert flow[today.isoformat()]["deliveries"] == 2 and flow[today.isoformat()]["bookings"] == 1

    # Tasks: new in the period, closed, and open / overdue now.
    mine = next(p for p in report["tasks"]["people"] if p["actorId"] == me)
    assert sum(mine["raised"]) >= 2 and mine["open"] >= 1 and mine["overdue"] >= 1

    # Only the tab that is open is read and sent.
    only = _build(s, tl, section="tasks")
    assert "tasks" in only and "pending" not in only and "flow" not in only

    # A PC's own view: their own pending work and delinquencies, no outlets.
    own = _build(s, me, own=True)
    assert own["pending"]["outlets"] == [] and [p["actorId"] for p in own["pending"]["people"]] == [me]
    assert "flow" not in own


def test_backlog_counts_unreconciled_payments_and_a_date_range_is_accepted(setup):
    s = setup
    tt._exec(s, """
        INSERT INTO auditcore.audit_findings (tenant_id, journey_id, finding_type_code, severity, finding_status, title,
          description, created_by_actor_id, stage_code, rule_key)
        VALUES (:t, :j, 'PAYMENT_UNVERIFIED', 'MEDIUM', 'OPEN', 'Payment not matched', 'x', 'system', 'DELIVERY', :k)
    """, j=s.journey.journey_id, k=f"PAYMENT_BANK_UNMATCHED:{uuid4()}")
    tl = f"tl-{uuid4().hex[:6]}"
    tt._role(s, tl, "TL")
    report = _build(s, tl, section="pending")
    at_outlet = next(o for o in report["pending"]["outlets"] if o["outletId"] == str(s.journey.outlet_id))["counts"]
    assert sum(at_outlet["paymentsNotReconciled"]) == 1
    assert "paymentsNotReconciled" in [k["key"] for k in report["pending"]["kinds"]]
    s.acting["subject"] = tl
    today = datetime.now(IST).date()
    url = f"/p2/v1/tenants/{s.journey.tenant_id}/reports/performance"
    body = s.client.get(url, params={"from": (today - timedelta(days=2)).isoformat(), "date": today.isoformat(),
                                     "section": "flow"})
    assert body.status_code == 200, body.text
    assert len(body.json()["days"]) == 3 and "flow" in body.json() and "pending" not in body.json()
    assert s.client.get(url, params={"from": (today + timedelta(days=1)).isoformat()}).status_code == 422


def test_how_the_upload_options_are_used(setup):
    s = setup
    add_batch_pages(s.journey, [("booking_form", "READY")])
    bulk, _ = add_batch_pages(s.journey, [("booking_form", "READY")])
    drive, _ = add_batch_pages(s.journey, [("booking_form", "READY")])
    tt._bulk_source(s, bulk, "WEB")
    tt._bulk_source(s, drive, "DRIVE")
    tl = f"tl-{uuid4().hex[:6]}"
    tt._role(s, tl, "TL")
    report = _build(s, tl, section="uploads")
    mine = next(p for p in report["uploads"]["people"] if p["actorId"] == s.journey.actor_id)
    assert (sum(mine["direct"]), sum(mine["bulk"]), sum(mine["drive"])) == (1, 1, 1)
    at_outlet = next(o for o in report["uploads"]["outlets"] if o["outletId"] == str(s.journey.outlet_id))
    assert sum(at_outlet["drive"]) == 1


def test_duplicates_and_quality_issues(setup):
    s = setup
    _batch, rows = add_batch_pages(s.journey, [("booking_form", "FAILED"), ("booking_form", "FAILED"),
                                               ("booking_form", "CANCELLED"), ("booking_form", "READY")])
    for row, code in zip(rows, ("DI_QUALITY_IMAGE_BLUR_SCORE", "DI_QUALITY_IMAGE_CONTRAST", "DUPLICATE_PAGE"),
                         strict=False):
        tt._exec(s, "UPDATE auditcore.p2_document_queue SET last_error=:c WHERE tenant_id=:t AND queue_id=:q",
                 c=code, q=row["queue_id"])
    tl = f"tl-{uuid4().hex[:6]}"
    tt._role(s, tl, "TL")
    report = _build(s, tl, section="quality")
    mine = next(p for p in report["quality"]["people"] if p["actorId"] == s.journey.actor_id)
    assert (mine["blurred"], mine["faded"], mine["duplicatePages"], mine["pagesUploaded"]) == (1, 1, 1, 4)
    at_outlet = next(o for o in report["quality"]["outlets"] if o["outletId"] == str(s.journey.outlet_id))
    assert at_outlet["blurred"] == 1 and at_outlet["pagesUploaded"] == 4


def _hide(s, *keys: str):
    with s.journey.engine.begin() as connection:
        for key in keys:
            connection.exec_driver_sql(
                "INSERT INTO platform_ops.performance_settings (item_key, shown, updated_by) VALUES (%s, false, 'sa-1') "
                "ON CONFLICT (item_key) DO UPDATE SET shown = false", (key,))


def test_the_dashboard_of_each_role(setup):
    """A PC and a Reviewer read their own work; a TL the team, a PM the Team Leads too; a
    SuperAdmin shows or hides each widget per role."""
    s = setup
    _clear_settings(s)
    outlet = str(s.journey.outlet_id)
    me = s.journey.actor_id
    today = datetime.now(IST).date()
    now = datetime.now(UTC)
    gate_pass_tests._delivery_file(s, journeys=2, pages=2)                  # two deliveries of mine today
    _uploads(s, me, tt._journey(s, outlet, me), 1)                           # and a booking
    old = tt._journey(s, outlet, me)
    batch, _rows = add_batch_pages(SimpleNamespace(**{**s.journey.__dict__, "journey_id": old}), [("gate_pass", "READY")])
    tt._exec(s, "UPDATE auditcore.p2_document_queue SET business_stage='DELIVERY', created_at_utc=:at "
                "WHERE tenant_id=:t AND batch_id=:b", at=now - timedelta(days=5), b=batch)
    tt._exec(s, "UPDATE auditcore.p2_upload_batches SET created_at_utc=:at WHERE tenant_id=:t AND batch_id=:b",
             at=now - timedelta(days=5), b=batch)
    tt._task(s, role="PC", journey=old, actor=me, created=now - timedelta(days=6), due=now - timedelta(days=5))
    schedule = tt._gate_schedule(s, today - timedelta(days=10))
    tt._task(s, role="PC", actor=me, scope="OUTLET", outlet=outlet, schedule=schedule,
             occurrence=today - timedelta(days=1), status="READY", created=now - timedelta(days=1, hours=3),
             due=now - timedelta(hours=2))
    reviewer, tl, pm = (f"{r}-{uuid4().hex[:6]}" for r in ("rv", "tl", "pm"))
    tt._role(s, reviewer, "REVIEWER", dealer=s.journey.dealer_id, outlet=s.journey.outlet_id)
    tt._role(s, tl, "TL")
    tt._role(s, pm, "PM")
    tt._closure(s, tt._journey(s, outlet, me), by=reviewer)
    yesterday = (today - timedelta(days=1)).isoformat()
    try:
        # ---- the PC: what I did, what waits for me, my duty day by day
        mine = _build(s, me, own=True, section="dashboard")
        assert "people" not in mine and "pending" not in mine          # one lean answer
        board = mine["dashboard"]
        assert board["view"] == "PC" and board["title"] == "My dashboard"
        tiles = {t["key"]: t for t in board["kpis"]["tiles"]}
        assert tiles["bookings"]["value"] == 1 and tiles["deliveries"]["value"] == 3
        waiting = {k["key"]: k for k in board["pending"]["kinds"]}
        assert waiting["deliveryFilesOpen"]["buckets"][2] == 1 and waiting["tasksOverdue"]["buckets"][2] == 1
        assert "awaitingReview" not in waiting
        [duty] = board["duties"]["rows"]
        assert duty["notDone"] == 1 and next(d for d in duty["days"] if d["date"] == yesterday)["notDone"] == 1
        assert [p["title"] for p in board["duties"]["planned"]] == [d["title"] for d in perf.PLANNED_DUTIES]
        assert sum(board["trend"]["series"][1]["data"]) == 3 and "scorecards" not in board and "attention" not in board

        # ---- the Reviewer: my reviews, what waits at my outlets
        theirs = _build(s, reviewer, own=True, section="dashboard")["dashboard"]
        assert theirs["view"] == "REVIEWER"
        assert {t["key"]: t["value"] for t in theirs["kpis"]["tiles"]}["closed"] == 1
        assert [k["key"] for k in theirs["pending"]["kinds"]] == ["awaitingReview", "awaitingDecision"]
        assert theirs["pending"]["kinds"][1]["total"] == 1             # my closure is with the TL / PM

        # ---- the Team Lead: the team, by name
        team = _build(s, tl, section="dashboard")["dashboard"]
        assert team["view"] == "TEAM"
        tiles = {t["key"]: t for t in team["kpis"]["tiles"]}
        assert tiles["bookings"]["value"] >= 1 and tiles["deliveries"]["value"] >= 3
        assert {k["key"] for k in team["pending"]["kinds"]} >= {"deliveryFilesOpen", "awaitingReview", "paymentsNotReconciled"}
        cards = {c["key"]: c for c in team["scorecards"]}
        assert {"pcs", "reviewers", "outlets"} <= set(cards) and "leads" not in cards
        row = next(r for r in cards["pcs"]["rows"] if r["id"] == me)
        # Overdue: the task five days late and yesterday's duty, which is an overdue task too.
        assert row["values"]["deliveries"] == 3 and row["values"]["overdue"] == 2 and row["values"]["dutiesMissed"] == 1
        assert len(row["spark"]) == len(team["days"])
        assert next(r for r in cards["reviewers"]["rows"] if r["id"] == reviewer)["values"]["closed"] == 1
        assert any("Duty missed" in line["text"] for line in team["attention"]["lines"])
        assert team["duties"]["rows"][0]["missedBy"][0]["dates"] == [yesterday]
        assert sum(team["tasks"]["raised"]) >= 2 and team["tasks"]["overdue"] >= 1
        assert set(team["channels"]) == {"direct", "bulk", "drive"} and team["quality"]["pagesUploaded"] > 0

        # ---- a PM also reads the Team Leads
        assert "leads" in {c["key"] for c in _build(s, pm, section="dashboard")["dashboard"]["scorecards"]}
        s.acting["subject"] = tl
        over_http = s.client.get(f"/p2/v1/tenants/{s.journey.tenant_id}/reports/performance", params={"section": "dashboard"})
        assert over_http.status_code == 200, over_http.text
        assert over_http.json()["dashboard"]["view"] == "TEAM" and over_http.json()["sections"]

        # ---- the SuperAdmin hides widgets and measures per role
        _hide(s, "PC:w.pending", "PC:w.duties", "TL:w.team", "TL:w.attention", "PM:activeMinutes")
        mine = _build(s, me, own=True, section="dashboard")["dashboard"]
        assert "pending" not in mine and "duties" not in mine and "kpis" in mine
        assert "pending" not in mine["widgets"] and "trend" in mine["widgets"]
        team = _build(s, tl, section="dashboard")["dashboard"]
        assert "scorecards" not in team and "attention" not in team and "pending" in team
        for_pm = _build(s, pm, section="dashboard")["dashboard"]
        pcs = next(c for c in for_pm["scorecards"] if c["key"] == "pcs")
        assert "hours" not in [c["key"] for c in pcs["columns"]] and "attention" in for_pm
    finally:
        _clear_settings(s)
