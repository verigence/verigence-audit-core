"""What each PC did on their daily duties, day by day (product owner 2026-11-24): gate register,
cash register, bank statements and photos by where they came from."""
from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from test_daily_duties import (  # noqa: F401
    MON,
    SAT,
    SUN,
    TUE,
    _at,
    _caller,
    _day,
    _Db,
    _on,
    _one,
    _tick,
    project,
)

from audit_core import daily_duties as dd
from audit_core.daily_duty_evidence import evidence_view


def _view(p, role="TL", start=MON, end=TUE, hh=19):
    with _Db(p) as c:
        return evidence_view(c, tenant_id=p.tenant, caller=_caller(p, role), start=start, end=end, now=_at(end, hh))


def _uploads(p, day, actor):
    j = p.journey
    at = _at(day, 15)
    with _Db(p) as c:
        c.execute(text("""
            INSERT INTO auditcore.p2_bank_statements (tenant_id, statement_id, dealer_id, period_from, period_to,
              file_name, file_format, uploaded_by, uploaded_at_utc)
            VALUES (:t, :id, :d, :day, :day, 'statement.xlsx', 'XLSX', :a, :at)
        """), {"t": p.tenant, "id": uuid4(), "d": j.dealer_id, "day": day, "a": actor, "at": at})
        for client, source in ((f"drive-{uuid4().hex}", "P2"), (f"drive-{uuid4().hex}", "P2"),
                               (f"app-{uuid4().hex}", "P2"), (f"extra-{uuid4().hex}", "P2_UNRECOGNISED")):
            c.execute(text("""
                INSERT INTO auditcore.delivery_vehicle_photos (tenant_id, journey_id, object_key, original_filename,
                  content_type, size_bytes, uploaded_by_actor_id, uploaded_at_utc, client_upload_id, capture_source)
                VALUES (:t, :j, :k, 'p.jpg', 'image/jpeg', 10, :a, :at, :client, :src)
            """), {"t": p.tenant, "j": j.journey_id, "k": f"k/{uuid4().hex}", "a": actor, "at": at,
                   "client": client, "src": source})


def _register(p, day: date, *, uploaded: date, counts: bool = True) -> None:
    with _Db(p) as c:
        c.execute(text("""
            INSERT INTO auditcore.p2_gate_passes (tenant_id, dealer_id, outlet_id, outlet_name, pc_actor_id, pc_name,
              business_date, object_key, content_type, size_bytes, width_px, height_px, sharpness, completed_at_utc,
              created_at_utc)
            VALUES (:t, :d, :o, 'O', :a, 'Pc One', :day, 'k', 'image/jpeg', 10, 100, 100, 50, :done, :at)
        """), {"t": p.tenant, "d": p.journey.dealer_id, "o": p.journey.outlet_id, "a": p.people["PC"],
               "day": day, "done": _at(uploaded, 11) if counts else None, "at": _at(uploaded, 10)})


def test_each_pc_day_by_day(project):  # noqa: F811
    p = project
    fri = SAT - timedelta(days=1)
    _on(p, MON)
    with _Db(p) as c:
        dd.add_standard_duties(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), now=_at(MON, 8))
    for day in (MON, TUE):
        _day(p, day=day)
    # Monday's list has Saturday's cash ledger (Sunday is not a working day): entered on Monday.
    _tick(p, _one(_day(p, day=MON), "Cash ledger"), day=MON, values={"amount": "25000", "transactions": "4"})
    _uploads(p, MON, p.people["PC"])
    # Gate registers, by the day each is for: Friday's uploaded on Monday (late), Saturday's on
    # Sunday, none for Sunday, Monday's uploaded on Tuesday with its counts still to enter.
    _register(p, fri, uploaded=MON)
    _register(p, SAT, uploaded=SUN)
    _register(p, MON, uploaded=TUE, counts=False)

    view = _view(p, start=fri, end=TUE)
    assert view["days"] == [(fri + timedelta(days=n)).isoformat() for n in range(5)]
    assert view["cashFrom"] == SAT.isoformat()
    me = next(r for r in view["pcs"] if r["actorId"] == p.people["PC"])
    by = {d["date"]: d for d in me["days"]}
    gate = {day: by[day.isoformat()]["gate"]["outlets"][0]["state"] for day in (fri, SAT, SUN, MON, TUE)}
    assert gate == {fri: "LATE", SAT: "DONE", SUN: "NOT_DONE", MON: "NO_COUNTS", TUE: "PENDING"}
    assert me["summary"]["gate"]["days"] == 4  # Tuesday's register is uploaded on Wednesday
    sat_cash = by[SAT.isoformat()]["cash"]
    assert (sat_cash["done"], sat_cash["outlets"][0]["amount"], sat_cash["outlets"][0]["transactions"]) == (
        1, 25000.0, 4.0)
    assert by[MON.isoformat()]["cash"]["pending"] == 1  # Monday's ledger is entered on Tuesday: not over
    assert by[fri.isoformat()]["cash"]["outlets"] == []  # before the duty started: not counted
    assert me["summary"]["cash"]["days"] == 1
    assert by[MON.isoformat()]["statements"] == 1 and by[TUE.isoformat()]["statements"] == 0
    assert by[MON.isoformat()]["photos"] == {"drive": 2, "camera": 1, "unread": 1}
    assert me["summary"]["cash"]["amount"] == 25000.0 and me["summary"]["statements"] == 1
    # The other PC of the outlet shares the outlet's registers, not this PC's uploads.
    other = next(r for r in view["pcs"] if r["actorId"] == p.people["PC2"])
    assert other["summary"]["gate"]["done"] == 1 and other["summary"]["statements"] == 0
    assert other["summary"]["photos"] == {"drive": 0, "camera": 0, "unread": 0}


def test_sunday_registers_before_the_rule_are_not_due(project):  # noqa: F811
    p = project
    _on(p, MON)
    old_sunday = SUN - timedelta(days=7)  # 15 Nov 2026: Monday's register still covered Sunday
    me = next(r for r in _view(p, start=old_sunday, end=old_sunday)["pcs"] if r["actorId"] == p.people["PC"])
    assert me["days"][0]["gate"]["outlets"] == [] and me["summary"]["gate"]["days"] == 0


def test_not_received_is_not_a_miss_and_a_pc_cannot_see_the_team(project):  # noqa: F811
    p = project
    _on(p, MON)
    with _Db(p) as c:
        dd.add_standard_duties(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), now=_at(MON, 8))
        item = _one(dd.my_day(c, tenant_id=p.tenant, caller=_caller(p, "PC"), now=_at(MON, 11)), "Cash ledger")
        dd.not_received(c, tenant_id=p.tenant, caller=_caller(p, "PC"), item_id=UUID(item["itemId"]),
                        comment=None, informed_group=True, now=_at(MON, 12))
    me = next(r for r in _view(p, start=SAT, end=TUE)["pcs"] if r["actorId"] == p.people["PC"])
    sat = next(d for d in me["days"] if d["date"] == SAT.isoformat())
    assert sat["cash"]["notReceived"] == 1 and sat["cash"]["notDone"] == 0
    with pytest.raises(dd.DutyForbidden):
        _view(p, role="PC")
