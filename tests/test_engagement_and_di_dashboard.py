"""Employee engagement (day / week / month, sessions, efficacy) and the DI
dashboard (outcomes, reasons in plain words, processing time, failed list)
(product owner 2026-10-27). Real Postgres."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_batch_pages,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import di_dashboard as di
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.engagement_reports import build_engagement, period_key, working_days
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.task_manager import Caller, TaskManagerError, ist_today
from audit_core.task_manager_reports import merge_sessions, record_activity


@pytest.fixture
def journey(monkeypatch):
    engine = database_engine()
    created = create_p2_journey(engine, prefix="eng")
    monkeypatch.setattr(di, "_signed", lambda key: f"https://storage.test/{key}" if key else None)
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _db(j):
    class _Ctx:
        def __enter__(self):
            self.cm = j.engine.begin()
            self.c = self.cm.__enter__()
            set_tenant_context(self.c, j.tenant_id)
            return self.c

        def __exit__(self, *exc):
            return self.cm.__exit__(*exc)
    return _Ctx()


def test_periods_and_working_days():
    from datetime import date
    assert period_key(date(2026, 10, 29), "week") == date(2026, 10, 26)
    assert period_key(date(2026, 10, 29), "month") == date(2026, 10, 1)
    assert working_days(date(2026, 10, 26), date(2026, 11, 1)) == 6  # Mon-Sun: no Sunday


def test_sessions_only_lengthen():
    a = {"start": "2026-10-27T03:00:00+00:00", "end": "2026-10-27T03:30:00+00:00", "activeSeconds": 900}
    later = {**a, "end": "2026-10-27T04:00:00+00:00", "activeSeconds": 1500}
    stale = {**a, "end": "2026-10-27T03:10:00+00:00", "activeSeconds": 100}
    merged = merge_sessions(merge_sessions([a], [later]), [stale])
    assert merged == [later]


def test_engagement_by_day_week_and_month(journey):
    today = ist_today()
    now = datetime.now(UTC)
    caller = Caller(actor_id=journey.actor_id, role="PC")
    s1 = (now - timedelta(minutes=90)).replace(microsecond=0)
    sessions = [
        {"start": s1.isoformat(), "end": (s1 + timedelta(minutes=40)).isoformat(), "activeSeconds": 1800},
        {"start": (s1 + timedelta(minutes=75)).isoformat(), "end": (s1 + timedelta(minutes=85)).isoformat(),
         "activeSeconds": 500},
    ]
    with _db(journey) as c:
        assert record_activity(c, tenant_id=journey.tenant_id, caller=caller, now=now, payload={
            "date": today.isoformat(), "activeSeconds": 3600, "idleSeconds": 1200, "backgroundSeconds": 0,
            "hourlyActiveMinutes": [0] * 24, "displayName": "Pc One", "sessions": sessions,
            "firstActiveAt": s1.isoformat(), "lastActiveAt": (s1 + timedelta(minutes=85)).isoformat()})
    add_batch_pages(journey, [("booking_form", "READY"), ("pan_card", "READY")])
    with _db(journey) as c:
        day = build_engagement(c, tenant_id=journey.tenant_id, start_day=today, end_day=today, period="day")
        month = build_engagement(c, tenant_id=journey.tenant_id, start_day=today - timedelta(days=40),
                                 end_day=today, period="month")
        week = build_engagement(c, tenant_id=journey.tenant_id, start_day=today - timedelta(days=13),
                                end_day=today, period="week")
    pc = next(p for p in day["people"] if p["actorId"] == journey.actor_id)
    t = pc["totals"]
    assert (t["activeMinutes"], t["idleMinutes"], t["sessions"]) == (60, 20, 2)
    assert t["sessionMinutes"] == {"min": 10, "avg": 25, "max": 40}
    assert t["filesUploaded"] == 1 and t["pagesUploaded"] == 2 and t["daysActive"] == 1
    assert t["efficacy"]["inUsePct"] == 75 and t["efficacy"]["filesPerActiveHour"] == 1.0
    assert len(month["periods"]) >= 2 and month["period"] == "month"
    assert len(week["periods"]) in (2, 3)
    pc_week = next(p for p in week["people"] if p["actorId"] == journey.actor_id)
    assert sum(p["sessions"] for p in pc_week["periods"]) == 2


def test_engagement_shows_where_the_time_was_spent(journey):
    """Product owner 2026-10-27: capture mobile too and show the source used.
    A phone's browser is its own source (MOBILE_WEB), added to the computer's
    time rather than merged with it."""
    today = ist_today()
    now = datetime.now(UTC)
    caller = Caller(actor_id=journey.actor_id, role="PC")
    with _db(journey) as c:
        for kind, seconds in (("WEB", 1800), ("MOBILE_WEB", 600), ("ANDROID", 1200)):
            assert record_activity(c, tenant_id=journey.tenant_id, caller=caller, now=now, payload={
                "date": today.isoformat(), "clientKind": kind, "activeSeconds": seconds, "idleSeconds": 0,
                "backgroundSeconds": 0, "hourlyActiveMinutes": [0] * 24})
        with pytest.raises(TaskManagerError):
            record_activity(c, tenant_id=journey.tenant_id, caller=caller, now=now, payload={
                "date": today.isoformat(), "clientKind": "TV", "activeSeconds": 1})
        report = build_engagement(c, tenant_id=journey.tenant_id, start_day=today, end_day=today, period="day")
    t = next(p for p in report["people"] if p["actorId"] == journey.actor_id)["totals"]
    assert t["activeMinutes"] == 60
    assert t["activeMinutesBySource"] == {"WEB": 30, "MOBILE_WEB": 10, "ANDROID": 20}


def test_engagement_gives_each_days_first_and_last_activity(journey):
    """Product owner 2026-10-27: crisp figures first, such as who started first
    and finished last each day; the page ranks people from these."""
    today = ist_today()
    now = datetime.now(UTC)
    start = now.replace(microsecond=0) - timedelta(hours=3)
    people = (("pc-early", 900, 0, 60), ("pc-mid", 3600, 30, 90), ("pc-late", 7200, 45, 150))
    with _db(journey) as c:
        for actor, seconds, begin, end in people:
            assert record_activity(c, tenant_id=journey.tenant_id, caller=Caller(actor_id=actor, role="PC"),
                                   now=now, payload={
                                       "date": today.isoformat(), "activeSeconds": seconds, "idleSeconds": 0,
                                       "backgroundSeconds": 0, "hourlyActiveMinutes": [0] * 24,
                                       "firstActiveAt": (start + timedelta(minutes=begin)).isoformat(),
                                       "lastActiveAt": (start + timedelta(minutes=end)).isoformat()})
        report = build_engagement(c, tenant_id=journey.tenant_id, start_day=today, end_day=today, period="day")
    by_actor = {p["actorId"]: p for p in report["people"]}
    days = {a: by_actor[a]["days"] for a in ("pc-early", "pc-mid", "pc-late")}
    assert all(len(d) == 1 and d[0]["day"] == today.isoformat() for d in days.values())
    assert days["pc-early"][0]["activeMinutes"] == 15
    assert days["pc-early"][0]["firstActiveAtUtc"] == start.isoformat()
    assert days["pc-late"][0]["lastActiveAtUtc"] == (start + timedelta(minutes=150)).isoformat()
    assert by_actor[journey.actor_id]["days"] == []  # mapped, no time today

def test_di_dashboard_outcomes_reasons_and_failed_links(journey):
    _, rows = add_batch_pages(journey, [("booking_form", "READY"), ("pan_card", "READY"), (None, "CANCELLED"),
                                        (None, "CANCELLED"), ("invoice", "SUPPORTING")])
    with _db(journey) as c:
        c.execute(text("UPDATE auditcore.p2_document_queue SET last_error='DI_QUALITY_IMAGE_BLUR_SCORE', "
                       "status_reason='blur' WHERE tenant_id=:t AND queue_id=:q"),
                  {"t": journey.tenant_id, "q": rows[2]["queue_id"]})
        c.execute(text("UPDATE auditcore.p2_document_queue SET last_error='NOT_READ_IN_30_MIN' "
                       "WHERE tenant_id=:t AND queue_id=:q"), {"t": journey.tenant_id, "q": rows[3]["queue_id"]})
        c.execute(text("UPDATE auditcore.p2_document_queue SET attempt_count=1 WHERE tenant_id=:t AND queue_id=:q"),
                  {"t": journey.tenant_id, "q": rows[1]["queue_id"]})
        today = ist_today()
        report = di.build_di_dashboard(c, tenant_id=journey.tenant_id, start_day=today, end_day=today)
    t = report["totals"]
    o = t["outcomes"]
    assert (t["files"], t["pages"], t["documents"]) == (1, 5, 5)
    assert (o["READ_FIRST"], o["READ_RETRIED"], o["EXTRA_COPY"], o["BAD_SCAN"], o["NOT_READ_IN_TIME"]) == (1, 1, 1, 1, 1)
    assert t["processingMinutes"]["count"] == 1 and t["avgPagesPerFile"] == 5.0
    assert {r["group"] for r in report["failureReasons"]} == {"Bad scan", "Processing delay"}
    assert len(report["failed"]) == 2 and all(f["openUrl"].startswith("https://storage.test/") for f in report["failed"])
    blur = next(f for f in report["failed"] if f["outcome"] == "BAD_SCAN")
    assert "Blurred" in blur["reason"] and blur["pageNumbers"] == [3]


def test_routes_are_executive_only(journey):
    acting = {"subject": journey.actor_id}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=acting["subject"])
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/reports"
    assert client.get(f"{base}/engagement").status_code == 403  # a PC
    assert client.get(f"{base}/di-dashboard").status_code == 403
    with _db(journey) as c:
        c.execute(text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                       "VALUES (:t, 'exec-1', 'EXECUTIVE')"), {"t": journey.tenant_id})
    acting["subject"] = "exec-1"
    assert client.get(f"{base}/engagement", params={"period": "week"}).status_code == 200
    assert client.get(f"{base}/di-dashboard").status_code == 200


# ------------------------------------------------- a person's day, minute by minute (2026-10-28)


def _states(**runs: str) -> str:
    """'.' everywhere, then each HH_MM=run of characters from that minute."""
    out = ["."] * 1440
    for key, run in runs.items():
        start = int(key[1:3]) * 60 + int(key[4:6])
        out[start:start + len(run)] = list(run)
    return "".join(out)


def test_the_day_in_blocks_and_the_longest_stretch_with_no_output():
    from audit_core.engagement_reports import day_analysis

    # 10:00 click, active to 10:03; idle 10:04-10:19; 10:20-10:59 scrolling only;
    # 11:00 click and output.
    states = _states(t10_00="KAAA" + "I" * 16 + "A" * 40, t11_00="KAAA")
    a = day_analysis(states, [600, 660])
    assert a["firstActive"] == "10:00" and a["lastActive"] == "11:03"
    assert a["longestNoOutput"]["minutes"] == 60 and a["longestNoOutput"]["from"] == "10:00"
    assert a["longestNoOutput"]["to"] == "11:00" and a["longestNoOutput"]["endsWithOutput"] is True
    assert [b["state"] for b in a["blocks"]] == ["WORKING", "ACTIVE_NO_OUTPUT", "ACTIVE_NO_OUTPUT", "WORKING"]
    assert a["blockCounts"] == {"WORKING": 2, "ACTIVE_NO_OUTPUT": 2, "IDLE": 0, "AWAY": 0}
    assert a["scrollOnly"] == [{"from": "10:20", "to": "11:00", "minutes": 40}]
    assert day_analysis(None, []) is None


def test_a_persons_day_shows_screens_output_and_blocks(journey):
    from audit_core.engagement_reports import person_day
    from audit_core.task_manager_reports import merge_minute_states

    day = ist_today() - timedelta(days=1)
    now = datetime.now(UTC)
    at = lambda hhmm: datetime.fromisoformat(f"{day.isoformat()}T{hhmm}:00+05:30")
    caller = Caller(actor_id=journey.actor_id, role="PC")
    first = _states(t10_00="KAAA" + "I" * 16)
    later = _states(t10_20="A" * 40, t11_00="KAAA")
    with _db(journey) as c:
        for states, visits in ((first, [{"at": at("10:00").isoformat(), "screen": "Bookings list"}]),
                               (later, [{"at": at("10:30").isoformat(), "screen": "Journey › Documents",
                                         "journeyId": str(journey.journey_id)}])):
            assert record_activity(c, tenant_id=journey.tenant_id, caller=caller, now=now, payload={
                "date": day.isoformat(), "activeSeconds": 2700, "idleSeconds": 960, "backgroundSeconds": 0,
                "hourlyActiveMinutes": [0] * 24, "minuteStates": states, "visits": visits,
                "firstActiveAt": at("10:00").isoformat(), "lastActiveAt": at("11:03").isoformat()})
        stored = c.execute(text("SELECT minute_states, screen_visits FROM auditcore.p2_user_activity_daily "
                                "WHERE tenant_id=:t AND actor_id=:a AND activity_date=:d"),
                           {"t": journey.tenant_id, "a": journey.actor_id, "d": day}).one()
    assert stored[0] == merge_minute_states(first, later) and len(stored[1]) == 2
    batch, _ = add_batch_pages(journey, [("booking_form", "READY")])
    with _db(journey) as c:
        c.execute(text("UPDATE auditcore.p2_upload_batches SET created_at_utc=:at, uploaded_by_actor_id=:a "
                       "WHERE batch_id=:b"), {"at": at("11:00"), "a": journey.actor_id, "b": batch})
        report = person_day(c, tenant_id=journey.tenant_id, actor_id=journey.actor_id, day=day)
    assert [o["label"] for o in report["outputs"]] == ["File uploaded"] and report["outputCount"] == 1
    assert report["analysis"]["longestNoOutput"]["minutes"] == 60
    assert [s["screen"] for s in report["screens"]] == ["Bookings list", "Journey › Documents"]
    assert report["screens"][1]["journeyReference"]
    minutes = {m["screen"]: m["activeMinutes"] for m in report["minutesByScreen"]}
    assert minutes == {"Bookings list": 4 + 10, "Journey › Documents": 30 + 4}


def test_person_day_is_executive_only(journey):
    acting = {"subject": journey.actor_id}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=acting["subject"])
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/p2/v1/tenants/{journey.tenant_id}/reports/engagement/person-day"
    assert client.get(url, params={"actorId": journey.actor_id}).status_code == 403
    with _db(journey) as c:
        c.execute(text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                       "VALUES (:t, 'exec-1', 'EXECUTIVE')"), {"t": journey.tenant_id})
    acting["subject"] = "exec-1"
    assert client.get(url, params={"actorId": journey.actor_id}).status_code == 200
