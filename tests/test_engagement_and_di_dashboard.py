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
from audit_core.task_manager import Caller, ist_today
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
