"""Where each PC, TL and PM opens Verigence from (product owner, 2026-11-06): each sign-in
or resume is logged against the person's tagged outlets (500 m), other places are grouped,
the top 3 get a readable address once, and only Executive and SuperAdmin read the summary.
Real Postgres (runtime role, so row security and the append-only grant are exercised);
Security and Google are doubles."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import AllowAllAuthorization, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_p2_access_locations as watch
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_object_keys import IST

OUTLET = (20.4900, 85.8800)
NEARBY = (20.4920, 85.8800)  # ~222 m: at the tagged outlet
HOME = (20.5188, 85.8800)  # ~3.2 km away
HOME_NEXT_DOOR = (20.5190, 85.8803)  # the same place as HOME
MARKET = (20.4600, 85.9000)
CAFE = (20.4300, 85.8500)
TEMPLE = (20.4000, 85.8000)


class FakeGoogle:
    def __init__(self):
        self.asked: list[tuple[float, float]] = []

    def address_for(self, latitude, longitude):
        self.asked.append((latitude, longitude))
        return f"Place near {latitude:.3f},{longitude:.3f}, Cuttack, Odisha"


def test_places_group_within_500_m_most_accesses_first():
    at = datetime(2026, 11, 7, 10, tzinfo=IST)
    places = watch.group_places([
        (*HOME, at), (*MARKET, at), (*HOME_NEXT_DOOR, at + timedelta(hours=1)), (*HOME, at),
    ])
    assert [p.count for p in places] == [3, 1]
    assert watch.haversine_m(places[0].latitude, places[0].longitude, *HOME) < 50
    assert watch.watch_open(datetime(2026, 11, 22, 23, 59, tzinfo=IST))
    assert not watch.watch_open(datetime(2026, 11, 23, 0, 0, tzinfo=IST))


@pytest.fixture
def setup(monkeypatch):
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="acc")
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
    people = {role: f"acc-{role.lower()}-{uuid4().hex[:8]}" for role in ("EXECUTIVE", "TL", "REVIEWER")}
    with engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        for role, actor in people.items():
            connection.execute(
                text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                     "VALUES (:t, :a, :r)"),
                {"t": journey.tenant_id, "a": actor, "r": role},
            )
    acting = {"subject": journey.actor_id}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=acting["subject"])
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    google = FakeGoogle()
    monkeypatch.setattr(watch, "get_outlet_geocoder", lambda: google)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield SimpleNamespace(journey=journey, client=client, acting=acting, people=people, engine=engine,
                              google=google, base=f"/p2/v1/tenants/{journey.tenant_id}/access-locations")
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()


def _located(lat_lon, kind="SIGN_IN", accuracy=25.0):
    return {"kind": kind, "outcome": "LOCATED", "latitude": lat_lon[0], "longitude": lat_lon[1],
            "accuracyM": accuracy, "source": "NATIVE", "deviceType": "ANDROID"}


def _seed(s, actor, role, points):
    """Accesses an hour apart (the API counts accesses less than 10 minutes apart once)."""
    with s.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        start = datetime.now(IST) - timedelta(hours=len(points) + 1)
        for i, point in enumerate(points):
            command = watch.AccessLocationCommand.model_validate(
                point if isinstance(point, dict) else _located(point))
            watch.record_access(connection, tenant_id=s.journey.tenant_id, actor_id=actor, role=role,
                                command=command, now=start + timedelta(hours=i))


def test_a_pc_sign_in_is_classified_against_their_tagged_outlet(setup):
    s = setup
    r = s.client.post(s.base, json=_located(NEARBY))
    assert r.status_code == 200, r.text
    assert r.json()["recorded"] is True and r.json()["atTaggedOutlet"] is True
    # a second open within 10 minutes is the same access
    again = s.client.post(s.base, json=_located(HOME, kind="RESUME"))
    assert again.json() == {"recorded": False, "reason": "RECENT", "accessId": None, "atTaggedOutlet": None}
    # a Reviewer is not watched
    s.acting["subject"] = s.people["REVIEWER"]
    assert s.client.post(s.base, json=_located(HOME)).json()["reason"] == "NOT_WATCHED"
    with s.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        row = connection.execute(
            text("SELECT role_code, nearest_outlet_name, distance_m, at_tagged_outlet FROM auditcore.p2_access_locations "
                 "WHERE tenant_id=:t"), {"t": s.journey.tenant_id}).mappings().one()
    assert row["role_code"] == "PC" and row["nearest_outlet_name"] == "Utkal Mahindra – CDA"
    assert float(row["distance_m"]) == pytest.approx(222, abs=2) and row["at_tagged_outlet"] is True


def test_recording_stops_after_22_november(setup):
    s = setup
    with s.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        result = watch.record_access(
            connection, tenant_id=s.journey.tenant_id, actor_id=s.journey.actor_id, role="PC",
            command=watch.AccessLocationCommand.model_validate(_located(HOME)),
            now=datetime(2026, 11, 23, 9, 0, tzinfo=IST),
        )
    assert result.recorded is False and result.reason == "WATCH_ENDED"


def test_executive_sees_counts_per_place_with_the_top_3_addressed_once(setup):
    s = setup
    pc = s.journey.actor_id
    _seed(s, pc, "PC", [NEARBY, NEARBY, HOME, HOME_NEXT_DOOR, HOME, MARKET, MARKET, CAFE, TEMPLE,
                        _located(HOME, accuracy=3000),
                        {"kind": "RESUME", "outcome": "DENIED", "source": "NATIVE"}])
    _seed(s, s.people["TL"], "TL", [HOME])
    s.acting["subject"] = s.people["EXECUTIVE"]
    r = s.client.get(s.base)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["watchUntil"] == "2026-11-22" and body["radiusM"] == 500
    me = next(p for p in body["people"] if p["actorId"] == pc)
    assert me["accesses"] == 11 and me["atTaggedOutlet"] == 2 and me["taggedOutlets"] == ["Utkal Mahindra – CDA"]
    assert me["elsewhere"] == 7 and me["unclear"] == 1 and me["noLocation"] == 1
    assert [p["count"] for p in me["topPlaces"]] == [3, 2, 1]
    assert me["places"] == 4 and me["otherPlacesAccesses"] == 1
    assert all(p["address"] and "Cuttack" in p["address"] for p in me["topPlaces"])
    asked = len(s.google.asked)
    # a second view reuses the kept addresses
    assert s.client.get(s.base).status_code == 200
    assert len(s.google.asked) == asked
    tl = s.client.get(s.base, params={"role": "TL"}).json()["people"]
    # a Project-wide TL is tagged to every outlet of the Project; their one access was 3 km away
    assert [p["actorId"] for p in tl] == [s.people["TL"]] and tl[0]["elsewhere"] == 1


def test_only_executive_and_super_admin_read_the_summary(setup):
    s = setup
    for role in ("TL", "REVIEWER"):
        s.acting["subject"] = s.people[role]
        assert s.client.get(s.base).status_code == 403
    s.acting["subject"] = s.journey.actor_id  # the PC
    assert s.client.get(s.base).status_code == 403


def test_the_log_is_append_only_and_kept_per_project(setup):
    s = setup
    _seed(s, s.journey.actor_id, "PC", [HOME])
    with s.engine.connect() as connection:
        connection.execute(text("SET ROLE audit_core_runtime"))
        set_tenant_context(connection, s.journey.tenant_id)
        with pytest.raises(Exception):
            connection.execute(text("UPDATE auditcore.p2_access_locations SET role_code='TL'"))
        connection.rollback()
        connection.execute(text("SET ROLE audit_core_runtime"))
        set_tenant_context(connection, "some-other-project")
        assert connection.execute(text("SELECT count(*) FROM auditcore.p2_access_locations")).scalar_one() == 0
