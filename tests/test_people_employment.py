"""People who have left, as HR reports them (2026-11-09). Real Postgres."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import people_employment
from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.dependencies import get_engine
from audit_core.main import app
from audit_core.people_employment import (
    end_leaver_roles,
    exit_dates,
    gone_on,
    store_leavers,
)
from audit_core.security import ServiceIntegrationPrincipal
from audit_core.task_manager_reports import build_utilisation
from audit_core.uc03_p2_object_keys import IST
from audit_core.uc03_pc_booking_documents import require_audit_service_principal


def test_a_leaver_counts_up_to_and_including_the_last_day():
    exits = {"gone": date(2026, 11, 4), "undated": None}
    assert not gone_on(exits, "gone", date(2026, 11, 4))
    assert gone_on(exits, "gone", date(2026, 11, 5))
    assert gone_on(exits, "undated", date(2000, 1, 1))
    assert not gone_on(exits, "still-here", date(2026, 11, 5))
    assert not gone_on(exits, None, date(2026, 11, 5))


@pytest.fixture()
def journey(monkeypatch):
    engine = database_engine()
    j = create_p2_journey(engine, prefix="leaver")
    monkeypatch.setattr(people_employment, "_PROJECTS_SQL",
                        text(f"SELECT tenant_id FROM auditcore.projects WHERE tenant_id = '{j.tenant_id}'"))
    with engine.begin() as c:
        set_tenant_context(c, j.tenant_id)
        c.execute(text("UPDATE auditcore.business_assignments SET effective_from = now() - interval '30 days' "
                       "WHERE tenant_id = :t"), {"t": j.tenant_id})
    yield j
    with engine.begin() as c:
        set_platform_super_admin_context(c)
        c.execute(text("DELETE FROM auditcore.people_employment"))
    delete_tenant_data(engine, j.tenant_id)
    engine.dispose()


def _roles(j):
    with j.engine.begin() as c:
        set_tenant_context(c, j.tenant_id)
        return c.execute(text("SELECT assignment_status, effective_to FROM auditcore.business_assignments "
                              "WHERE tenant_id = :t AND security_actor_id = :a"),
                         {"t": j.tenant_id, "a": j.actor_id}).all()


def _today() -> date:
    return datetime.now(UTC).astimezone(IST).date()


def test_roles_end_at_the_end_of_the_last_day_and_not_before(journey):
    today = _today()
    store_leavers(journey.engine, [{"actorId": journey.actor_id, "status": "QUIT", "exitDate": today}])
    assert end_leaver_roles(journey.engine) == 0  # today is still a working day
    assert [r[0] for r in _roles(journey)] == ["ACTIVE"]

    last_day = today - timedelta(days=3)
    store_leavers(journey.engine, [{"actorId": journey.actor_id, "status": "QUIT", "exitDate": last_day}])
    assert end_leaver_roles(journey.engine) == 1
    (status, ended_at), = _roles(journey)
    assert status == "INACTIVE"
    assert ended_at.astimezone(IST) == datetime.combine(last_day + timedelta(days=1), datetime.min.time(), IST)
    assert end_leaver_roles(journey.engine) == 0  # nothing left to end


def test_reports_keep_a_leaver_only_up_to_the_last_day(journey):
    today = _today()
    yesterday = today - timedelta(days=1)
    store_leavers(journey.engine, [{"actorId": journey.actor_id, "status": "TERMINATED", "exitDate": yesterday}])
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        assert exit_dates(c)[journey.actor_id] == yesterday
        after = build_utilisation(c, tenant_id=journey.tenant_id, start_day=today, end_day=today)
        spanning = build_utilisation(c, tenant_id=journey.tenant_id, start_day=yesterday, end_day=today)
    assert journey.actor_id not in {p["actorId"] for p in after["people"]}
    assert journey.actor_id in {p["actorId"] for p in spanning["people"]}


def test_only_hr_may_report_and_a_report_replaces_the_list(journey):
    client = TestClient(app)
    app.dependency_overrides[get_engine] = lambda: journey.engine
    try:
        body = {"leavers": [
            {"securityUserId": journey.actor_id, "employeeCode": "JBR099", "status": "SUSPENDED",
             "exitDate": (_today() - timedelta(days=2)).isoformat()},
            {"securityUserId": "someone-active", "status": "ACTIVE"},
        ]}
        app.dependency_overrides[require_audit_service_principal] = lambda: ServiceIntegrationPrincipal(
            subject="someone-else")
        assert client.put("/v1/service/hr/employment", json=body).status_code == 403

        app.dependency_overrides[require_audit_service_principal] = lambda: ServiceIntegrationPrincipal(
            subject="hrmgmt")
        answer = client.put("/v1/service/hr/employment", json=body)
        assert answer.status_code == 200, answer.text
        assert answer.json()["leavers"] == 1 and answer.json()["rolesEnded"] == 1
        with journey.engine.begin() as c:
            assert set(exit_dates(c)) == {journey.actor_id}

        # Made active again: the next report no longer lists them.
        assert client.put("/v1/service/hr/employment", json={"leavers": []}).json()["leavers"] == 0
        with journey.engine.begin() as c:
            assert exit_dates(c) == {}
    finally:
        app.dependency_overrides.clear()
