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


def _build(s, actor: str, *, own: bool = False, admin: bool = False, day: date | None = None):
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        return perf.build_performance(connection, tenant_id=s.journey.tenant_id, actor_id=actor,
                                      day=day or datetime.now(IST).date(), own=own, admin=admin, use_cache=False)


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
