"""What each PC did on their daily duties, day by day (product owner 2026-11-24): gate register,
cash register, bank statements and photos by where they came from."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from test_daily_duties import (  # noqa: F401
    MON,
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


def test_each_pc_day_by_day(project):  # noqa: F811
    p = project
    _on(p, MON)
    with _Db(p) as c:
        dd.add_standard_duties(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), now=_at(MON, 8))
    for day in (MON, TUE):
        _day(p, day=day)
    # Monday: the cash register entered; Tuesday: nothing. Neither day a gate register.
    _tick(p, _one(_day(p, day=MON), "Cash ledger"), day=MON, values={"amount": "25000", "transactions": "4"})
    _uploads(p, MON, p.people["PC"])

    view = _view(p)
    assert view["days"] == [MON.isoformat(), TUE.isoformat()]
    me = next(r for r in view["pcs"] if r["actorId"] == p.people["PC"])
    mon, tue = me["days"]
    assert (mon["cash"]["done"], mon["cash"]["outlets"][0]["amount"], mon["cash"]["outlets"][0]["transactions"]) == (
        1, 25000.0, 4.0)
    assert tue["cash"]["done"] == 0 and tue["cash"]["pending"] == 1  # today: not over yet
    assert mon["gate"]["notDone"] == 1  # a past day, not done
    assert mon["statements"] == 1 and tue["statements"] == 0
    assert mon["photos"] == {"drive": 2, "camera": 1, "unread": 1}
    assert me["summary"]["cash"]["amount"] == 25000.0 and me["summary"]["statements"] == 1
    # The other PC of the outlet shares the outlet's duties, not this PC's uploads.
    other = next(r for r in view["pcs"] if r["actorId"] == p.people["PC2"])
    assert other["days"][0]["cash"]["done"] == 1 and other["days"][0]["statements"] == 0
    assert other["summary"]["photos"] == {"drive": 0, "camera": 0, "unread": 0}


def test_not_received_is_not_a_miss_and_a_pc_cannot_see_the_team(project):  # noqa: F811
    p = project
    _on(p, MON)
    with _Db(p) as c:
        dd.add_standard_duties(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), now=_at(MON, 8))
        item = _one(dd.my_day(c, tenant_id=p.tenant, caller=_caller(p, "PC"), now=_at(MON, 11)), "Cash ledger")
        dd.not_received(c, tenant_id=p.tenant, caller=_caller(p, "PC"), item_id=UUID(item["itemId"]),
                        comment=None, informed_group=True, now=_at(MON, 12))
    me = next(r for r in _view(p, end=TUE)["pcs"] if r["actorId"] == p.people["PC"])
    assert me["days"][0]["cash"]["notReceived"] == 1 and me["days"][0]["cash"]["notDone"] == 0
    with pytest.raises(dd.DutyForbidden):
        _view(p, role="PC")
