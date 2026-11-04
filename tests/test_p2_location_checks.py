"""DealGuard location checks (product owner, 2026-11-05): the distance is worked
out on the server against the PC's own outlets, the result is classified and
logged, and TL / PM / Executive read the day's log. Real Postgres (the API runs
as the runtime role, so row security and the append-only grant are exercised);
Security is a double."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import AllowAllAuthorization, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_p2_location_checks as checks
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_engine, get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_object_keys import IST

# Utkal Mahindra, CDA Cuttack (roughly) and points around it.
OUTLET = (20.4900, 85.8800)
NEARBY = (20.4920, 85.8800)  # ~222 m north
FAR = (20.5188, 85.8800)  # ~3.2 km north
REAL_IN_WINDOW = checks.in_window


def _ist(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 5, hour, minute, tzinfo=IST)


def test_haversine_and_classification():
    assert checks.haversine_m(*OUTLET, *OUTLET) == 0
    assert checks.haversine_m(*OUTLET, *FAR) == pytest.approx(3202, abs=5)
    outlet = {"outlet_id": uuid4(), "outlet_name": "CDA", "dealer_name": "Utkal Mahindra",
              "latitude": OUTLET[0], "longitude": OUTLET[1]}
    other = {**outlet, "outlet_id": uuid4(), "latitude": 21.0, "longitude": 86.0}
    near = checks.classify(latitude=NEARBY[0], longitude=NEARBY[1], accuracy_m=20, outlets=[other, outlet])
    assert near["status"] == "AT_OUTLET" and near["outlet"] is outlet and near["distance_m"] == pytest.approx(222, abs=2)
    assert checks.classify(latitude=FAR[0], longitude=FAR[1], accuracy_m=30, outlets=[outlet])["status"] == "AWAY"
    # A desktop's Wi-Fi / IP reading is never AWAY, however far it points.
    assert checks.classify(latitude=FAR[0], longitude=FAR[1], accuracy_m=1500, outlets=[outlet])["status"] == "UNCLEAR"
    assert checks.classify(latitude=FAR[0], longitude=FAR[1], accuracy_m=None, outlets=[outlet])["status"] == "UNCLEAR"
    assert checks.classify(latitude=FAR[0], longitude=FAR[1], accuracy_m=10, outlets=[])["status"] == "NO_OUTLET_LOCATION"
    assert checks.GEOFENCE_RADIUS_M == 500 and checks.ACCURACY_LIMIT_M == 1000


def test_outlet_label_and_window():
    assert checks.outlet_label("CDA", "Utkal Mahindra") == "Utkal Mahindra – CDA"
    assert checks.outlet_label("Utkal Mahindra CDA", "Utkal Mahindra") == "Utkal Mahindra CDA"
    assert checks.outlet_label(None, "Utkal Mahindra") == "Utkal Mahindra"
    assert not checks.in_window(_ist(9, 59))
    assert checks.in_window(_ist(10, 0))
    assert checks.in_window(_ist(19, 29))
    assert checks.in_window(_ist(19, 34))  # a check started before 19:30, arriving late
    assert not checks.in_window(_ist(19, 35))
    assert not checks.in_window(_ist(21, 0))


@pytest.fixture
def setup(monkeypatch):
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="loc")
    with engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.dealer_outlets SET latitude=:lat, longitude=:lon, outlet_name='CDA' "
                 "WHERE tenant_id=:t AND outlet_id=:o"),
            {"lat": OUTLET[0], "lon": OUTLET[1], "t": journey.tenant_id, "o": journey.outlet_id},
        )
        connection.execute(
            text("UPDATE auditcore.dealers SET dealer_name='Utkal Mahindra' WHERE tenant_id=:t AND dealer_id=:d"),
            {"t": journey.tenant_id, "d": journey.dealer_id},
        )
    tl = f"loc-tl-{uuid4().hex[:8]}"
    with engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                 "VALUES (:t, :a, 'TL')"),
            {"t": journey.tenant_id, "a": tl},
        )
    acting = {"subject": journey.actor_id}
    authorization = AllowAllAuthorization()
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=acting["subject"])
    app.dependency_overrides[get_security_authorization_client] = lambda: authorization
    # The window is tested on its own; the API tests must not depend on the time they run.
    monkeypatch.setattr(checks, "in_window", lambda now: True)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield SimpleNamespace(journey=journey, client=client, acting=acting, tl=tl, engine=engine,
                              authorization=authorization,
                              base=f"/p2/v1/tenants/{journey.tenant_id}/location-checks")
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()


def _located(lat_lon, accuracy=25.0, **extra):
    return {"outcome": "LOCATED", "latitude": lat_lon[0], "longitude": lat_lon[1], "accuracyM": accuracy,
            "capturedAt": "2026-11-05T06:00:00Z", "source": "NATIVE", "deviceType": "ANDROID", **extra}


def _rows(s):
    with s.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        return [dict(r) for r in connection.execute(
            text("SELECT * FROM auditcore.p2_location_checks WHERE tenant_id=:t ORDER BY checked_at_utc"),
            {"t": s.journey.tenant_id},
        ).mappings()]


def test_pc_check_is_classified_on_the_server_and_logged(setup):
    s = setup
    at = s.client.post(s.base, json=_located(NEARBY))
    assert at.status_code == 200, at.text
    assert at.json()["recorded"] is True and at.json()["status"] == "AT_OUTLET"

    # A distance sent by the device is ignored: the server works it out.
    away = s.client.post(s.base, json=_located(FAR, distanceM=0, status="AT_OUTLET"))
    body = away.json()
    assert body["status"] == "AWAY"
    assert body["distanceM"] == pytest.approx(3202, abs=5)
    assert body["outletName"] == "Utkal Mahindra – CDA"
    assert body["outletId"] == str(s.journey.outlet_id)

    denied = s.client.post(s.base, json={"outcome": "DENIED", "source": "BROWSER", "deviceType": "WEB",
                                         "detail": "User denied Geolocation"})
    assert denied.json()["recorded"] is True and denied.json()["status"] == "DENIED"
    assert denied.json()["distanceM"] is None and denied.json()["outletName"] is None

    rows = _rows(s)
    assert [r["check_status"] for r in rows] == ["AT_OUTLET", "AWAY", "DENIED"]
    assert rows[1]["actor_id"] == s.journey.actor_id
    assert float(rows[1]["distance_m"]) == pytest.approx(3202, abs=5)
    assert rows[1]["nearest_outlet_id"] == s.journey.outlet_id
    assert rows[1]["location_source"] == "NATIVE" and rows[1]["device_type"] == "ANDROID"
    assert rows[2]["latitude"] is None and rows[2]["client_detail"] == "User denied Geolocation"


def test_poor_accuracy_and_missing_outlet_coordinates(setup):
    s = setup
    assert s.client.post(s.base, json=_located(FAR, accuracy=2500, source="BROWSER")).json()["status"] == "UNCLEAR"
    with s.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("UPDATE auditcore.dealer_outlets SET latitude=NULL, longitude=NULL WHERE tenant_id=:t"),
                           {"t": s.journey.tenant_id})
    assert s.client.post(s.base, json=_located(FAR)).json()["status"] == "NO_OUTLET_LOCATION"


def test_bad_requests_and_non_pc_are_refused(setup):
    s = setup
    assert s.client.post(s.base, json={"outcome": "LOCATED", "source": "NATIVE"}).status_code == 400
    assert s.client.post(s.base, json=_located((95.0, 85.0))).status_code == 400
    s.acting["subject"] = s.tl
    assert s.client.post(s.base, json=_located(FAR)).status_code == 403
    assert _rows(s) == []


def test_hours_and_hourly_limit_are_enforced_by_the_server(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(checks, "in_window", REAL_IN_WINDOW)
    command = checks.LocationCheckCommand(**_located(FAR))
    with s.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        late = checks.record_location_check(connection, tenant_id=s.journey.tenant_id,
                                            actor_id=s.journey.actor_id, command=command, now=_ist(21, 5))
    assert late.recorded is False and late.reason == "OUTSIDE_HOURS"
    results = []
    for minute in (2, 20, 40, 55):
        with s.engine.begin() as connection:
            set_tenant_context(connection, s.journey.tenant_id)
            results.append(checks.record_location_check(
                connection, tenant_id=s.journey.tenant_id, actor_id=s.journey.actor_id, command=command,
                now=_ist(11, minute)))
    assert [r.recorded for r in results] == [True, True, True, False]
    assert results[-1].reason == "HOURLY_LIMIT"
    with s.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        next_hour = checks.record_location_check(connection, tenant_id=s.journey.tenant_id,
                                                 actor_id=s.journey.actor_id, command=command, now=_ist(12, 1))
    assert next_hour.recorded is True
    assert len(_rows(s)) == 4


def test_late_evening_check_is_not_recorded(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(checks, "in_window", lambda now: False)
    response = s.client.post(s.base, json=_located(FAR))
    assert response.status_code == 200
    assert response.json()["recorded"] is False and response.json()["reason"] == "OUTSIDE_HOURS"
    assert _rows(s) == []


def test_team_lead_reads_the_days_log(setup):
    s = setup
    s.client.post(s.base, json=_located(NEARBY))
    s.client.post(s.base, json=_located(FAR))
    s.client.post(s.base, json=_located(FAR))

    # A PC does not read the log.
    assert s.client.get(s.base).status_code == 403

    s.acting["subject"] = s.tl
    log = s.client.get(s.base)
    assert log.status_code == 200, log.text
    body = log.json()
    assert body["radiusM"] == 500 and body["window"] == {"from": "10:00", "to": "19:30"}
    assert body["totals"]["checks"] == 3 and body["totals"]["AWAY"] == 2
    [person] = body["people"]
    assert person["actorId"] == s.journey.actor_id
    assert person["checks"] == 3 and person["away"] == 2 and person["counts"]["AT_OUTLET"] == 1
    assert person["last"]["status"] == "AWAY" and person["last"]["outletName"] == "Utkal Mahindra – CDA"
    assert len(person["items"]) == 3
    assert "latitude" not in person["items"][0]

    away_only = s.client.get(s.base, params={"awayOnly": "true"}).json()
    assert [i["status"] for i in away_only["people"][0]["items"]] == ["AWAY", "AWAY"]
    assert away_only["people"][0]["checks"] == 3

    other_day = s.client.get(s.base, params={"day": "2026-01-01"}).json()
    assert other_day["people"] == [] and other_day["day"] == "2026-01-01"


def test_dealer_scoped_team_lead_sees_only_their_pcs(setup):
    s = setup
    s.client.post(s.base, json=_located(FAR))
    with s.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        other_dealer = connection.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'Other') RETURNING dealer_id"),
            {"t": s.journey.tenant_id, "c": f"OTHER-{uuid4().hex[:6]}"},
        ).scalar_one()
        connection.execute(
            text("UPDATE auditcore.business_assignments SET dealer_id=:d WHERE tenant_id=:t AND security_actor_id=:a"),
            {"d": other_dealer, "t": s.journey.tenant_id, "a": s.tl},
        )
    s.acting["subject"] = s.tl
    assert s.client.get(s.base).json()["people"] == []
    with s.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.business_assignments SET dealer_id=:d WHERE tenant_id=:t AND security_actor_id=:a"),
            {"d": s.journey.dealer_id, "t": s.journey.tenant_id, "a": s.tl},
        )
    assert len(s.client.get(s.base).json()["people"]) == 1


def test_log_is_append_only_and_tenant_isolated(setup):
    s = setup
    s.client.post(s.base, json=_located(FAR))
    runtime = get_engine()
    with runtime.begin() as connection:
        set_tenant_context(connection, f"tenant-other-{uuid4().hex[:6]}")
        assert connection.execute(text("SELECT count(*) FROM auditcore.p2_location_checks")).scalar_one() == 0
    for statement in ("UPDATE auditcore.p2_location_checks SET check_status='AT_OUTLET'",
                      "DELETE FROM auditcore.p2_location_checks"):
        with pytest.raises(Exception, match="permission denied"), runtime.begin() as connection:
            set_tenant_context(connection, s.journey.tenant_id)
            connection.execute(text(statement))
    assert len(_rows(s)) == 1
