"""AI cost on the SuperAdmin System Health page (product owner 2026-11-16: "capture
the cost"). Document Intelligence prices each provider call and answers per day,
Project, kind of call and document type; Audit Core adds the Projects' names and the
Journeys the spend was for, and never estimates a call that kept nothing."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from p2_support import add_page, create_p2_journey, database_engine

from audit_core import system_health_ai_cost as ai_cost
from audit_core import system_health_di
from audit_core.dependencies import HumanAdminRequest, require_super_admin_request
from audit_core.main import app
from audit_core.security_integration import SecurityAdminContext
from audit_core.uc03_p2_object_keys import IST

MODEL = "gemini-3-flash-preview"


def _row(day: date, tenant: str, capability: str, **over):
    row = {"date": day.isoformat(), "tenantId": tenant, "capability": capability,
           "documentTypeKey": "booking_form" if capability == "EXTRACTION" else None, "model": MODEL,
           "tier": "STANDARD", "calls": 10, "failedCalls": 0, "callsWithoutUsage": 0, "promptTokens": 40_000,
           "outputTokens": 15_000, "costUsd": 0.065, "costFromTokensUsd": 0.0}
    return {**row, **over}


def _build(rows, *, today=date(2026, 11, 16), days=30, tenant_id=None, journeys=None, names=None):
    return ai_cost.build_ai_cost(
        {"rows": rows, "prices": [], "classificationRecordedSinceUtc": None},
        names=names or {"t1": {"name": "Mahindra Orissa", "status": "ACTIVE"}},
        journeys=journeys or {"t1": 20}, tenant_id=tenant_id, days=days, today=today, rate=94.0)


def test_every_day_of_the_window_is_there_and_reading_is_kept_apart_from_sorting():
    today = date(2026, 11, 16)
    body = _build([
        _row(today, "t1", "EXTRACTION", costUsd=0.5, calls=100, failedCalls=2),
        _row(today, "t1", "CLASSIFICATION", costUsd=0.1, calls=110, promptTokens=220_000, outputTokens=4_000),
        _row(today - timedelta(days=1), "t1", "EXTRACTION", costUsd=0.4, calls=80),
    ], today=today, days=7)
    assert [d["date"] for d in body["daily"]] == [(today - timedelta(days=n)).isoformat() for n in range(6, -1, -1)]
    assert body["daily"][0]["costUsd"] == 0.0  # a day with no calls is a zero, not a gap
    last = body["daily"][-1]
    assert (last["extractionUsd"], last["classificationUsd"], last["costUsd"]) == (0.5, 0.1, 0.6)
    assert (last["reads"], last["classifications"], last["failedCalls"]) == (100, 110, 2)
    assert body["today"] == last
    assert body["totals"]["costUsd"] == 1.0 and body["totals"]["reads"] == 180
    assert (body["usdInr"], body["currency"], body["timezone"]) == (94.0, "USD", "Asia/Kolkata")


def test_a_call_that_kept_nothing_is_counted_and_never_costed():
    today = date(2026, 11, 16)
    body = _build([
        _row(today, "t1", "EXTRACTION", costUsd=0.5, calls=100, callsWithoutUsage=3),
        # Sorting calls from before their tokens were kept.
        _row(today, "t1", "CLASSIFICATION", model=None, tier=None, costUsd=None, calls=400,
             callsWithoutUsage=400, promptTokens=0, outputTokens=0),
        # A read whose model has no price.
        _row(today, "t1", "EXTRACTION", model="unknown", documentTypeKey="unknown", costUsd=None, calls=5),
    ], today=today, days=1)
    totals = body["totals"]
    assert totals["costUsd"] == 0.5
    assert totals["callsWithoutCost"] == 3 + 400 + 5
    assert (totals["reads"], totals["classifications"]) == (105, 400)
    assert any("counted, not estimated" in n for n in body["notes"])


def test_what_was_priced_from_old_tokens_is_said():
    today = date(2026, 11, 16)
    body = _build([_row(today, "t1", "EXTRACTION", costUsd=35.0, costFromTokensUsd=34.81)], today=today, days=1)
    assert body["totals"]["costFromTokensUsd"] == 34.81
    assert any("priced from the tokens" in n and "34.81" in n for n in body["notes"])
    assert not any("priced from the tokens" in n for n in _build([_row(today, "t1", "EXTRACTION")], days=1)["notes"])


def test_projects_carry_their_name_and_the_cost_per_journey_with_uploads():
    today = date(2026, 11, 16)
    body = _build(
        [_row(today, "t1", "EXTRACTION", costUsd=8.0), _row(today, "t1", "CLASSIFICATION", costUsd=2.0),
         _row(today, "t2", "EXTRACTION", costUsd=1.0)],
        today=today, days=1, journeys={"t1": 100, "t2": 0},
        names={"t1": {"name": "Mahindra Orissa", "status": "ACTIVE"}, "t2": {"name": "Hyundai Orissa", "status": "ACTIVE"}})
    first, second = body["projects"]
    assert (first["projectName"], first["costUsd"], first["journeysWithUploads"], first["costPerJourneyUsd"]) == (
        "Mahindra Orissa", 10.0, 100, 0.1)
    assert (second["projectName"], second["costPerJourneyUsd"]) == ("Hyundai Orissa", None)  # no Journeys: no rate
    only = _build([_row(today, "t1", "EXTRACTION", costUsd=8.0), _row(today, "t2", "EXTRACTION", costUsd=1.0)],
                  today=today, days=1, tenant_id="t2")
    assert only["totals"]["costUsd"] == 1.0 and [p["tenantId"] for p in only["projects"]] == ["t2"]


def test_document_types_are_reads_only_dearest_first_with_the_cost_of_one_read():
    today = date(2026, 11, 16)
    body = _build([
        _row(today, "t1", "EXTRACTION", documentTypeKey="customer_ledger", costUsd=3.0, calls=300),
        _row(today, "t1", "EXTRACTION", documentTypeKey="booking_form", costUsd=5.0, calls=1000),
        _row(today, "t1", "CLASSIFICATION", costUsd=9.0, calls=2000),
    ], today=today, days=1)
    assert [(t["documentTypeKey"], t["costUsd"], t["costPerReadUsd"]) for t in body["documentTypes"]] == [
        ("booking_form", 5.0, 0.005), ("customer_ledger", 3.0, 0.01)]


def test_the_month_is_projected_at_the_pace_of_its_complete_days_only():
    today = date(2026, 11, 16)
    rows = [_row(today - timedelta(days=n), "t1", "EXTRACTION", costUsd=5.0) for n in range(1, 16)]  # 1-15 Nov
    rows.append(_row(today, "t1", "EXTRACTION", costUsd=1.0))                                        # today, part day
    month = _build(rows, today=today, days=30)["month"]
    assert (month["month"], month["coveredFromFirst"], month["completeDays"], month["daysInMonth"]) == (
        "2026-11", True, 15, 30)
    assert month["costUsd"] == 76.0 and month["projectedUsd"] == 150.0  # 5 a day for 30 days; today not in the pace
    # A window that does not reach the 1st, or too few complete days: no projection.
    assert _build(rows, today=today, days=7)["month"]["projectedUsd"] is None
    early = date(2026, 11, 2)
    assert _build([_row(early, "t1", "EXTRACTION")], today=early, days=30)["month"]["projectedUsd"] is None


def test_the_rupee_rate_comes_from_the_service_and_a_bad_one_is_not_used(monkeypatch):
    monkeypatch.delenv("AUDIT_CORE_USD_INR", raising=False)
    assert ai_cost.usd_inr() == ai_cost.DEFAULT_USD_INR
    monkeypatch.setenv("AUDIT_CORE_USD_INR", "95.5")
    assert ai_cost.usd_inr() == 95.5
    for bad in ("abc", "0", "-3"):
        monkeypatch.setenv("AUDIT_CORE_USD_INR", bad)
        assert ai_cost.usd_inr() == ai_cost.DEFAULT_USD_INR


def test_di_is_asked_for_usage_with_a_di_service_token(monkeypatch):
    seen = {}

    class FakeOAuth:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def get_service_token(self, *, audience):
            seen["audience"] = audience
            return "svc-token"

    def fake_get(url, *, params, headers, timeout):
        seen.update(url=url, params=params, auth=headers["Authorization"])
        return httpx.Response(200, json={"rows": []}, request=httpx.Request("GET", url))

    for key, value in {"DI_BASE_URL": "https://di.example/", "SECURITY_BASE_URL": "https://sec.example",
                       "SECURITY_CLIENT_ID": "audit-core", "SECURITY_CLIENT_SECRET": "x"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(system_health_di, "SecurityOAuthClient", FakeOAuth)
    monkeypatch.setattr(system_health_di.httpx, "get", fake_get)
    assert system_health_di.fetch_di_ai_usage(30, "Asia/Kolkata") == {"rows": []}
    assert seen == {"audience": "di", "url": "https://di.example/v1/platform/ai-usage",
                    "params": {"days": 30, "tz": "Asia/Kolkata"}, "auth": "Bearer svc-token"}
    # A DI without the route yet (404), or an answer in another shape, is "unavailable", not a crash.
    monkeypatch.setattr(system_health_di.httpx, "get",
                        lambda url, **kw: httpx.Response(404, request=httpx.Request("GET", url)))
    with pytest.raises(system_health_di.DiHealthUnavailable, match="404"):
        system_health_di.fetch_di_ai_usage(30, "Asia/Kolkata")
    monkeypatch.setattr(system_health_di.httpx, "get",
                        lambda url, **kw: httpx.Response(200, json={"boots": []}, request=httpx.Request("GET", url)))
    with pytest.raises(system_health_di.DiHealthUnavailable, match="unexpected shape"):
        system_health_di.fetch_di_ai_usage(30, "Asia/Kolkata")


def _get(monkeypatch, fake, params, *, super_admin=True):
    monkeypatch.setattr(ai_cost, "fetch_di_ai_usage", fake)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        if super_admin:
            app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
                user_id="sa", bearer_token="t",
                admin_context=SecurityAdminContext(user_id="sa", is_super_admin=True, admin_scopes=()))
        return client.get("/v1/admin/system-health/ai-cost", params=params)
    finally:
        app.dependency_overrides.clear()


def test_the_page_names_the_project_and_counts_the_journeys_the_spend_was_for(monkeypatch):
    engine = database_engine()
    try:
        journey = create_p2_journey(engine, prefix="aicost")
        add_page(journey, page_number=1, status="READY")
        add_page(journey, page_number=2, status="READY")
        tenant = str(journey.tenant_id)
        today = datetime.now(UTC).astimezone(IST).date()
        asked = {}

        def fake(days, tz):
            asked.update(days=days, tz=tz)
            return {"rows": [_row(today, tenant, "EXTRACTION", costUsd=0.02, calls=2),
                             _row(today, tenant, "CLASSIFICATION", costUsd=0.004, calls=2)],
                    "prices": [{"model": MODEL, "tier": "STANDARD", "inputPerMTok": 0.5, "outputPerMTok": 3.0}],
                    "classificationRecordedSinceUtc": "2026-11-16T18:00:00+00:00"}

        response = _get(monkeypatch, fake, {"days": 7, "tenantId": tenant})
        assert response.status_code == 200, response.text
        body = response.json()
        assert asked == {"days": 7, "tz": "Asia/Kolkata"}
        assert body["available"] is True and body["error"] is None and len(body["daily"]) == 7
        [mine] = body["projects"]
        assert mine["projectName"] != tenant  # named, not a bare id
        assert (mine["journeysWithUploads"], mine["costUsd"], mine["costPerJourneyUsd"]) == (1, 0.024, 0.024)
        assert body["prices"][0]["model"] == MODEL
        assert any(p["tenantId"] == tenant for p in body["projectOptions"])
        assert str(journey.journey_id) not in json.dumps(body)  # amounts only
    finally:
        engine.dispose()


def test_an_unreachable_document_intelligence_is_said_on_the_page_not_an_error(monkeypatch):
    def down(days, tz):
        raise system_health_di.DiHealthUnavailable("Document Intelligence answered HTTP 404.")

    response = _get(monkeypatch, down, {"days": 3})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is False and "404" in body["error"]
    assert body["totals"]["costUsd"] == 0.0 and len(body["daily"]) == 3


def test_without_superadmin_the_cost_is_refused(monkeypatch):
    response = _get(monkeypatch, lambda days, tz: {"rows": []}, {"days": 3}, super_admin=False)
    assert response.status_code in (401, 403)
