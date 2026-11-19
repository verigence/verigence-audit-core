"""Daily Duties (product owner 2026-11-18, docs/DAILY_DUTIES_DESIGN_v1.0.md): the
switch, the duty list, the PC's list, ticks and numbers, "not received today" and
the WhatsApp group question, day close with reasons, carry-forward, what the system
reads for itself, and the team's view (real Postgres)."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

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

from audit_core import daily_duties as dd
from audit_core import daily_duty_checkers
from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_human_principal,
    require_super_admin_request,
)
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.security_integration import SecurityAdminContext
from audit_core.task_manager import SUPER_ADMIN, Caller

IST = dd.IST
MON = date(2026, 11, 23)  # a Monday
TUE, WED, SAT, SUN = MON + timedelta(days=1), MON + timedelta(days=2), MON - timedelta(days=2), MON - timedelta(days=1)


def _at(day: date, hh: int = 11, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=IST).astimezone(UTC)


@pytest.fixture
def project():
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="dd")
    t = journey.tenant_id
    people = {"PC": journey.actor_id}
    with engine.begin() as connection:
        for role in ("TL", "PM", "EXECUTIVE"):
            actor = f"dd-{role.lower()}-{uuid4().hex[:6]}"
            people[role] = actor
            connection.execute(
                text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                     "VALUES (:t, :a, :r)"), {"t": t, "a": actor, "r": role})
        pc2 = f"dd-pc2-{uuid4().hex[:6]}"
        people["PC2"] = pc2
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id, outlet_id) VALUES (:t, :a, 'PC', :d, :o)"),
            {"t": t, "a": pc2, "d": journey.dealer_id, "o": journey.outlet_id})
    try:
        yield SimpleNamespace(engine=engine, tenant=t, journey=journey, people=people)
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, t)
        engine.dispose()


def _caller(p, role: str) -> Caller:
    if role == SUPER_ADMIN:
        return Caller(actor_id="super-admin-1", role=SUPER_ADMIN, display_name="Super Admin")
    return Caller(actor_id=p.people[role], role="PC" if role == "PC2" else role, display_name=f"{role} Name")


class _Db:
    def __init__(self, p):
        self.p = p

    def __enter__(self):
        self.cm = self.p.engine.begin()
        self.c = self.cm.__enter__()
        set_tenant_context(self.c, self.p.tenant)
        return self.c

    def __exit__(self, *exc):
        return self.cm.__exit__(*exc)


def _on(p, day: date = MON, **settings) -> None:
    with _Db(p) as c:
        dd.update_settings(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"),
                           spec={"enabled": True, **settings}, now=_at(day, 8))


def _duty(p, day: date = MON, **spec) -> dict:
    with _Db(p) as c:
        return dd.create_duty(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"),
                              spec={"title": "Count the stock", **spec}, now=_at(day, 8))


def _day(p, role: str = "PC", day: date = MON, hh: int = 11) -> dict:
    with _Db(p) as c:
        return dd.my_day(c, tenant_id=p.tenant, caller=_caller(p, role), now=_at(day, hh))


def _items(view: dict) -> list[dict]:
    return [i for o in view.get("outlets", []) for i in o["items"]]


def _one(view: dict, title: str = "Count the stock") -> dict:
    [item] = [i for i in _items(view) if i["title"] == title]
    return item


def _tick(p, item: dict, role: str = "PC", day: date = MON, hh: int = 12, values=None, comment=None) -> dict:
    with _Db(p) as c:
        return dd.tick(c, tenant_id=p.tenant, caller=_caller(p, role), item_id=UUID(item["itemId"]), values=values,
                       comment=comment, now=_at(day, hh))


def _close(p, reasons=(), role: str = "PC", day: date = MON, hh: int = 18) -> dict:
    with _Db(p) as c:
        return dd.close_day(c, tenant_id=p.tenant, caller=_caller(p, role), reasons=list(reasons), now=_at(day, hh))


def _team(p, role: str = "TL", start: date = MON, end: date = MON, day: date | None = None, hh: int = 19) -> dict:
    with _Db(p) as c:
        return dd.team_view(c, tenant_id=p.tenant, caller=_caller(p, role), start=start, end=end,
                            now=_at(day or end, hh))


# --------------------------------------------------------------------------- the switch


def test_off_by_default_and_nothing_is_created(project):
    p = project
    _duty(p)
    view = _day(p)
    assert view == {"enabled": False, "date": MON.isoformat(), "role": "PC", "applies": False}
    assert _team(p)["enabled"] is False
    with p.engine.connect() as c:
        set_tenant_context(c, p.tenant)
        assert c.execute(text("SELECT count(*) FROM auditcore.p2_duty_items WHERE tenant_id = :t"),
                         {"t": p.tenant}).scalar_one() == 0


def test_only_an_executive_or_superadmin_keeps_the_switch_and_the_list(project):
    p = project
    for role in ("PC", "TL", "PM"):
        with pytest.raises(dd.DutyForbidden), _Db(p) as c:
            dd.update_settings(c, tenant_id=p.tenant, caller=_caller(p, role), spec={"enabled": True})
        with pytest.raises(dd.DutyForbidden), _Db(p) as c:
            dd.create_duty(c, tenant_id=p.tenant, caller=_caller(p, role), spec={"title": "x"})
    with _Db(p) as c:
        assert dd.update_settings(c, tenant_id=p.tenant, caller=_caller(p, SUPER_ADMIN),
                                  spec={"enabled": True, "cutoffTimeIst": "20:30", "defaultCarryDays": 3},
                                  now=_at(MON, 8)) | {"updatedAt": None} == {
            "enabled": True, "enabledFrom": MON.isoformat(), "cutoffTimeIst": "20:30", "defaultCarryDays": 3,
            "workingWeekdays": [1, 2, 3, 4, 5, 6], "updatedByName": "Super Admin", "updatedAt": None,
            "canManage": True}
    with pytest.raises(dd.DutyError), _Db(p) as c:
        dd.update_settings(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), spec={"cutoffTimeIst": "9pm"})


def test_switching_off_takes_everything_off_the_lists_and_on_again_starts_fresh(project):
    p = project
    _on(p)
    _duty(p)
    assert _one(_day(p))["status"] == "PENDING"
    with _Db(p) as c:
        dd.update_settings(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), spec={"enabled": False},
                           now=_at(MON, 15))
    assert _day(p, hh=16)["enabled"] is False
    with pytest.raises(dd.DutyConflict), _Db(p) as c:
        dd.close_day(c, tenant_id=p.tenant, caller=_caller(p, "PC"), reasons=[], now=_at(MON, 18))
    _on(p, day=WED)
    view = _day(p, day=WED)
    # Monday's item was closed as switched off; Tuesday was never created; Wednesday is new.
    assert [(i["dutyDate"], i["status"]) for i in _items(view)] == [(WED.isoformat(), "PENDING")]
    assert _team(p, start=MON, end=WED)["totals"]["incomplete"] == 0


# --------------------------------------------------------------------------- the duty list


def test_the_standard_duties_are_added_once_and_each_can_be_changed(project):
    p = project
    with _Db(p) as c:
        assert dd.add_standard_duties(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), now=_at(MON, 8)) == {
            "added": 7}
        assert dd.add_standard_duties(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), now=_at(MON, 8)) == {
            "added": 0}
        listed = dd.list_duties(c, tenant_id=p.tenant, caller=_caller(p, "TL"))
    assert [d["standardCode"] for d in listed["items"]] == [
        "GATE_REGISTER", "INTIMATION", "UPLOADS", "PHYSICAL_VERIFICATION", "BANK_STATEMENT", "CASH_LEDGER",
        "OPEN_TASKS"]
    assert listed["canManage"] is False and listed["standardMissing"] == []
    by_code = {d["standardCode"]: d for d in listed["items"]}
    # Asked of the PC: the files received and the cash ledger. Read from the system: the rest.
    assert {c: d["closeMode"] for c, d in by_code.items()} == {
        "GATE_REGISTER": "AUTO", "INTIMATION": "TICK", "UPLOADS": "AUTO", "PHYSICAL_VERIFICATION": "AUTO",
        "BANK_STATEMENT": "AUTO", "CASH_LEDGER": "TICK", "OPEN_TASKS": "AUTO"}
    # A day's work on the tasks is not made up the next day.
    assert by_code["OPEN_TASKS"]["carryDays"] == 0 and by_code["OPEN_TASKS"]["checkerCode"] == "OPEN_TASKS"
    assert by_code["CASH_LEDGER"]["dayOffset"] == -1 and by_code["CASH_LEDGER"]["allowNotReceived"]
    assert by_code["BANK_STATEMENT"]["rolling"] and by_code["GATE_REGISTER"]["weekdays"] == [1, 2, 3, 4, 5, 6, 7]
    with _Db(p) as c:
        changed = dd.update_duty(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"),
                                 duty_id=UUID(by_code["CASH_LEDGER"]["dutyId"]),
                                 spec={"mandatory": False, "carryDays": 2}, now=_at(MON, 9))
    assert changed["mandatory"] is False and changed["carryDays"] == 2 and changed["title"] == "Cash ledger"


def test_a_duty_is_checked_when_it_is_written(project):
    p = project
    bad = [
        {"title": " "}, {"weekdays": []}, {"weekdays": [0]}, {"dayOffset": -2}, {"closeMode": "AUTO"},
        {"closeMode": "AUTO", "checkerCode": "NOPE"}, {"outletScope": "SELECTED"},
        {"outletScope": "SELECTED", "outletIds": [str(uuid4())]}, {"inputs": [{"label": "", "type": "NUMBER"}]},
        {"inputs": [{"label": "x", "type": "DATE"}]}, {"carryDays": 31},
        {"closeMode": "AUTO", "checkerCode": "GATE_REGISTER", "inputs": [{"label": "n", "type": "NUMBER"}]},
        {"effectiveFrom": (MON - timedelta(days=1)).isoformat()},
    ]
    for spec in bad:
        with pytest.raises(dd.DutyError), _Db(p) as c:
            dd.create_duty(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), spec={"title": "x", **spec},
                           now=_at(MON, 8))
    made = _duty(p, inputs=[{"label": "Vehicles in stock", "type": "NUMBER"},
                            {"label": "Vehicles in stock", "type": "NUMBER", "required": False}])
    assert [i["key"] for i in made["inputs"]] == ["vehicles_in_stock", "vehicles_in_stock_2"]


def test_a_change_shows_on_days_not_started_and_an_inactive_duty_leaves_every_list(project):
    p = project
    _on(p)
    made = _duty(p)
    assert _one(_day(p))["mandatory"] is True
    with _Db(p) as c:
        dd.update_duty(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), duty_id=UUID(made["dutyId"]),
                       spec={"mandatory": False, "title": "Count the stock yard"}, now=_at(MON, 13))
    assert _one(_day(p, hh=14))["mandatory"] is True  # Monday's item keeps Monday's duty
    tuesday = _day(p, day=TUE)
    assert {(i["title"], i["mandatory"], i["dutyDate"]) for i in _items(tuesday)} == {
        ("Count the stock", True, MON.isoformat()), ("Count the stock yard", False, TUE.isoformat())}
    with _Db(p) as c:
        dd.update_duty(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), duty_id=UUID(made["dutyId"]),
                       spec={"status": "INACTIVE"}, now=_at(TUE, 13))
    assert _day(p, day=TUE, hh=14)["outlets"] == [] and _day(p, day=WED)["outlets"] == []


def test_outlet_scope(project):
    p = project
    _on(p)
    _duty(p, title="Onsite only", outletScope="ONSITE")
    _duty(p, title="Satellite only", outletScope="SATELLITE")
    _duty(p, title="Chosen", outletScope="SELECTED", outletIds=[str(p.journey.outlet_id)])
    assert sorted(i["title"] for i in _items(_day(p))) == ["Chosen", "Onsite only"]


# --------------------------------------------------------------------------- the PC's list


def test_other_roles_have_no_list(project):
    p = project
    _on(p)
    _duty(p)
    for role in ("TL", "PM", "EXECUTIVE"):
        assert _day(p, role)["applies"] is False
    with pytest.raises(dd.DutyForbidden), _Db(p) as c:
        dd.close_day(c, tenant_id=p.tenant, caller=_caller(p, "TL"), reasons=[], now=_at(MON, 18))


def test_tick_with_numbers_and_take_it_back_the_same_day(project):
    p = project
    _on(p)
    _duty(p, title="Cash ledger", inputs=[{"key": "amount", "label": "Amount", "type": "AMOUNT"},
                                         {"key": "transactions", "label": "Transactions", "type": "NUMBER"},
                                         {"key": "note", "label": "Note", "type": "TEXT", "required": False}])
    item = _one(_day(p), "Cash ledger")
    assert item["canTick"] and not item["canUndo"] and item["daysLeft"] == 5
    for values in ({}, {"amount": "abc", "transactions": 3}, {"amount": -1, "transactions": 3},
                   {"amount": 100, "transactions": 2.5}):
        with pytest.raises(dd.DutyError):
            _tick(p, item, values=values)
    done = _one(_tick(p, item, values={"amount": "1,25,000.50", "transactions": "7"}, comment=" tallied "),
                "Cash ledger")
    assert done["status"] == "DONE" and done["values"] == {"amount": 125000.5, "transactions": 7}
    assert done["comment"] == "tallied" and done["doneByName"] == "PC Name" and done["canUndo"]
    with pytest.raises(dd.DutyConflict):
        _tick(p, item, values={"amount": 1, "transactions": 1})
    # The other PC of the outlet sees it done and cannot take it back.
    theirs = _one(_day(p, "PC2"), "Cash ledger")
    assert theirs["status"] == "DONE" and not theirs["canUndo"]
    with pytest.raises(dd.DutyConflict), _Db(p) as c:
        dd.undo(c, tenant_id=p.tenant, caller=_caller(p, "PC2"), item_id=UUID(item["itemId"]), now=_at(MON, 13))
    with _Db(p) as c:
        back = dd.undo(c, tenant_id=p.tenant, caller=_caller(p, "PC"), item_id=UUID(item["itemId"]),
                       now=_at(MON, 13))
    assert _one(back, "Cash ledger")["status"] == "PENDING" and _one(back, "Cash ledger")["values"] == {}
    _tick(p, item, values={"amount": 5, "transactions": 1})
    with pytest.raises(dd.DutyConflict), _Db(p) as c:  # not on a later day
        dd.undo(c, tenant_id=p.tenant, caller=_caller(p, "PC"), item_id=UUID(item["itemId"]), now=_at(TUE, 10))


def test_a_pc_of_another_outlet_cannot_touch_the_item(project):
    p = project
    _on(p)
    _duty(p)
    item = _one(_day(p))
    with p.engine.begin() as c:
        other = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, 'X2', 'Other') RETURNING outlet_id"),
            {"t": p.tenant, "d": p.journey.dealer_id}).scalar_one()
        c.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id, outlet_id) VALUES (:t, 'dd-stranger', 'PC', :d, :o)"),
            {"t": p.tenant, "d": p.journey.dealer_id, "o": other})
    stranger = Caller(actor_id="dd-stranger", role="PC", display_name="Stranger")
    with pytest.raises(dd.DutyNotFound), _Db(p) as c:
        dd.tick(c, tenant_id=p.tenant, caller=stranger, item_id=UUID(item["itemId"]), values=None, comment=None,
                now=_at(MON, 12))
    with _Db(p) as c:
        own = dd.my_day(c, tenant_id=p.tenant, caller=stranger, now=_at(MON, 12))
    assert [o["outletName"] for o in own["outlets"]] == ["Other"]


def test_a_previous_day_duty_and_the_weekend(project):
    p = project
    _on(p, day=SAT)
    _duty(p, day=SAT, title="Register", dayOffset=-1, weekdays=[1, 2, 3, 4, 5, 6, 7])
    _duty(p, day=SAT, title="Uploads")
    saturday = _day(p, day=SAT)
    assert {(i["title"], i["forDate"]) for i in _items(saturday)} == {
        ("Register", (SAT - timedelta(days=1)).isoformat()), ("Uploads", SAT.isoformat())}
    # Sunday is not a working day: Saturday's and Sunday's registers both come on Monday.
    monday = _day(p, day=MON)
    assert sorted((i["title"], i["forDate"], i["dutyDate"]) for i in _items(monday) if i["dutyDate"] == MON.isoformat()) == [
        ("Register", SAT.isoformat(), MON.isoformat()), ("Register", SUN.isoformat(), MON.isoformat()),
        ("Uploads", MON.isoformat(), MON.isoformat())]
    carried = [i for i in _items(monday) if i["dutyDate"] != MON.isoformat()]
    assert {(i["title"], i["carriedDays"], i["daysLeft"]) for i in carried} == {("Register", 2, 3), ("Uploads", 2, 3)}


# --------------------------------------------------------------------------- not received, reasons, day close


def test_not_received_asks_about_the_whatsapp_group_and_is_asked_again_next_day(project):
    p = project
    _on(p)
    _duty(p, title="Cash ledger", allowNotReceived=True)
    _duty(p, title="Stock")
    view = _day(p)
    with pytest.raises(dd.DutyConflict), _Db(p) as c:  # only where the duty allows it
        dd.not_received(c, tenant_id=p.tenant, caller=_caller(p, "PC"), item_id=UUID(_one(view, "Stock")["itemId"]),
                        comment=None, informed_group=True, now=_at(MON, 12))
    cash = _one(view, "Cash ledger")
    with pytest.raises(dd.DutyError, match="WhatsApp"), _Db(p) as c:
        dd.not_received(c, tenant_id=p.tenant, caller=_caller(p, "PC"), item_id=UUID(cash["itemId"]),
                        comment=None, informed_group=None, now=_at(MON, 12))
    with _Db(p) as c:
        after = dd.not_received(c, tenant_id=p.tenant, caller=_caller(p, "PC"), item_id=UUID(cash["itemId"]),
                                comment="Accountant on leave", informed_group=False, now=_at(MON, 12))
    marked = _one(after, "Cash ledger")
    assert marked["status"] == "NOT_RECEIVED" and marked["canTick"]
    assert marked["reasons"][0] | {"at": None} == {
        "category": "NOT_RECEIVED", "label": "Not received today", "side": "DEALERSHIP",
        "remark": "Accountant on leave", "byActorId": p.people["PC"], "byName": "PC Name",
        "handledByActorId": None, "handledByName": None, "informedGroup": False, "at": None}
    # It covers the day for every PC of the outlet: only "Stock" still needs a reason.
    assert after["counts"]["needReason"] == 1 and _day(p, "PC2")["counts"]["needReason"] == 1
    tuesday = next(i for i in _items(_day(p, day=TUE))
                   if i["title"] == "Cash ledger" and i["dutyDate"] == MON.isoformat())
    assert tuesday["status"] == "PENDING" and tuesday["carriedDays"] == 1  # asked again
    cell = next(i for i in _team(p)["days"][0]["outlets"][0]["items"] if i["title"] == "Cash ledger")
    assert cell["state"] == "NOT_RECEIVED" and cell["reasons"][0]["informedGroup"] is False


def test_day_close_needs_a_reason_for_every_mandatory_duty_not_done(project):
    p = project
    _on(p)
    _duty(p, title="A")
    _duty(p, title="B")
    _duty(p, title="Optional", mandatory=False)
    view = _day(p)
    a, b = _one(view, "A"), _one(view, "B")
    assert view["counts"] == {"total": 3, "done": 0, "onTrack": 0, "pending": 3, "mandatoryPending": 2,
                              "needReason": 2}
    with pytest.raises(dd.DutyError, match="Give a reason for: A; B"):
        _close(p)
    with pytest.raises(dd.DutyError, match="choose a reason"):
        _close(p, [{"itemId": a["itemId"], "category": "NOPE"}])
    with pytest.raises(dd.DutyError, match="say why"):
        _close(p, [{"itemId": a["itemId"], "category": "OTHER"}])
    with pytest.raises(dd.DutyError, match="choose the PC"):
        _close(p, [{"itemId": a["itemId"], "category": "HANDLED_BY_OTHER_PC", "handledByActorId": p.people["TL"]}])
    _tick(p, b)
    closed = _close(p, [{"itemId": a["itemId"], "category": "HANDLED_BY_OTHER_PC",
                         "handledByActorId": p.people["PC2"], "remark": "She has the papers"}])
    assert closed["dayClosedAt"] is not None and closed["counts"]["needReason"] == 0
    reason = _one(closed, "A")["myReason"]
    assert (reason["category"], reason["side"], reason["handledByActorId"]) == (
        "HANDLED_BY_OTHER_PC", "PC", p.people["PC2"])
    # A PC who finishes later the same day can still tick, and closing again keeps the first time.
    _tick(p, a, hh=20)
    again = _close(p, hh=21)
    assert again["dayClosedAt"] == closed["dayClosedAt"]
    team = _team(p, hh=22)
    [outlet] = team["days"][0]["outlets"]
    assert {pc["actorId"]: pc["dayClose"] for pc in outlet["pcs"]} == {p.people["PC"]: "CLOSED",
                                                                       p.people["PC2"]: "NOT_CLOSED"}
    assert team["totals"]["done"] == 2 and team["totals"]["pending"] == 1  # the optional one


def test_nothing_pending_closes_without_reasons(project):
    p = project
    _on(p)
    _duty(p)
    _tick(p, _one(_day(p)))
    assert _close(p)["dayClosedAt"] is not None


# --------------------------------------------------------------------------- carry-forward


def test_an_item_is_carried_for_its_days_then_is_incomplete(project):
    p = project
    _on(p, defaultCarryDays=2)
    _duty(p, weekdays=[1])  # Mondays only
    item = _one(_day(p))
    assert _one(_day(p, day=WED))["carriedDays"] == 2 and _one(_day(p, day=WED))["daysLeft"] == 0
    thursday = MON + timedelta(days=3)
    assert _day(p, day=thursday)["outlets"] == []  # off the PC's list
    with pytest.raises(dd.DutyConflict, match="carry days are over"):
        _tick(p, item, day=thursday)
    team = _team(p, "PM", start=MON, end=thursday)
    states = {d["date"]: [i["state"] for o in d["outlets"] for i in o["items"]] for d in team["days"]}
    assert states == {thursday.isoformat(): [], WED.isoformat(): ["INCOMPLETE"], TUE.isoformat(): ["PENDING"],
                      MON.isoformat(): ["PENDING"]}
    assert team["totals"] == {"done": 0, "doneLate": 0, "onTrack": 0, "pending": 0, "notReceived": 0,
                              "incomplete": 1}


def test_done_on_a_later_day_is_done_late(project):
    p = project
    _on(p)
    _duty(p, weekdays=[1])
    _tick(p, _one(_day(p)), day=TUE)
    team = _team(p, start=MON, end=TUE)
    states = {d["date"]: [i["state"] for o in d["outlets"] for i in o["items"]] for d in team["days"]}
    assert states == {TUE.isoformat(): ["DONE_LATE"], MON.isoformat(): ["PENDING"]}
    assert team["byDuty"][0] | {"dutyId": None, "sortOrder": None} == {
        "dutyId": None, "title": "Count the stock", "sortOrder": None, "done": 0, "doneLate": 1, "onTrack": 0,
        "pending": 0, "notReceived": 0, "incomplete": 0}


def test_a_rolling_duty_has_one_open_item_at_a_time(project):
    p = project
    _on(p, defaultCarryDays=2)
    _duty(p, title="Bank statement", rolling=True, allowNotReceived=True)
    first = _one(_day(p), "Bank statement")
    assert [i["dutyDate"] for i in _items(_day(p, day=TUE))] == [MON.isoformat()]
    assert [i["dutyDate"] for i in _items(_day(p, day=WED))] == [MON.isoformat()]
    thursday = MON + timedelta(days=3)  # Monday's ran out on Wednesday: a new one
    assert [i["dutyDate"] for i in _items(_day(p, day=thursday))] == [thursday.isoformat()]
    assert first["itemId"] != _items(_day(p, day=thursday))[0]["itemId"]


# --------------------------------------------------------------------------- what the system reads


def _gate_register(p, day: date, *, finished: bool) -> None:
    with _Db(p) as c:
        c.execute(text("""
            INSERT INTO auditcore.p2_gate_passes (tenant_id, dealer_id, outlet_id, outlet_name, pc_actor_id, pc_name,
              business_date, object_key, content_type, size_bytes, width_px, height_px, sharpness, completed_at_utc)
            VALUES (:t, :d, :o, 'O', :a, 'Pc One', :day, 'k', 'image/jpeg', 10, 100, 100, 50, :done)
        """), {"t": p.tenant, "d": p.journey.dealer_id, "o": p.journey.outlet_id, "a": p.people["PC"],
               "day": day, "done": _at(day + timedelta(days=1), 10) if finished else None})


def test_the_gate_register_ticks_itself(project):
    p = project
    _on(p)
    _duty(p, title="Gate out register", dayOffset=-1, weekdays=[1, 2, 3, 4, 5, 6, 7], closeMode="AUTO",
          checkerCode="GATE_REGISTER")
    view = _day(p)
    saturday = next(i for i in _items(view) if i["forDate"] == SAT.isoformat())
    assert saturday["status"] == "PENDING" and not saturday["canTick"]
    assert saturday["progress"] == {"state": "PENDING", "note": "Register of 21 Nov is not uploaded", "done": None,
                                    "total": None}
    with pytest.raises(dd.DutyConflict, match="ticks itself"):
        _tick(p, saturday)
    _gate_register(p, SAT, finished=False)
    waiting = next(i for i in _items(_day(p)) if i["forDate"] == SAT.isoformat())
    assert waiting["progress"]["note"] == "Register uploaded; its counts are not entered yet"
    _gate_register(p, SUN, finished=True)
    done = next(i for i in _items(_day(p)) if i["forDate"] == SUN.isoformat())
    assert (done["status"], done["doneSource"], done["doneByName"], done["canUndo"]) == ("DONE", "SYSTEM", "Pc One",
                                                                                         False)
    # The reason is asked only for the register still missing.
    assert _day(p)["counts"]["needReason"] == 1


def _delivery(p, day: date, *, photo_at: datetime | None = None) -> UUID:
    """A Journey of the outlet with its vehicle gate pass dated ``day``, and a vehicle photo when asked."""
    document = uuid4()
    with _Db(p) as c:
        journey = c.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference) "
                 "VALUES (:t, :d, :o, :c, :r) RETURNING journey_id"),
            {"t": p.tenant, "d": p.journey.dealer_id, "o": p.journey.outlet_id, "c": p.journey.customer_id,
             "r": f"DD-{uuid4().hex[:8]}"}).scalar_one()
        c.execute(
            text("INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id, di_document_id, "
                 "document_type_key, evidence_purpose, process_area, association_status) "
                 "VALUES (:t, :j, :c, :s, :d, 'gate_pass', 'JOURNEY_DOCUMENT', 'DELIVERY', 'ACTIVE')"),
            {"t": p.tenant, "j": journey, "c": p.journey.customer_id, "s": str(uuid4()), "d": str(document)})
        c.execute(
            text("INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id, di_document_id, "
                 "field_key, stage_code, source_document_type_key, extracted_value, effective_value, is_modified) "
                 "VALUES (:t, :j, :d, 'delivery_date', 'DELIVERY', 'gate_pass', CAST(:v AS jsonb), CAST(:v AS jsonb), "
                 "false)"),
            {"t": p.tenant, "j": journey, "d": str(document), "v": json.dumps(day.isoformat())})
    if photo_at is not None:
        _photo(p, journey, photo_at)
    return UUID(str(journey))


def _photo(p, journey, at: datetime, view_code: str | None = None) -> None:
    with _Db(p) as c:
        c.execute(
            text("INSERT INTO auditcore.delivery_vehicle_photos (tenant_id, journey_id, object_key, original_filename, "
                 "content_type, size_bytes, uploaded_by_actor_id, uploaded_at_utc, view_code) "
                 "VALUES (:t, :j, :k, 'car.jpg', 'image/jpeg', 10, :a, :at, :v)"),
            {"t": p.tenant, "j": journey, "k": f"k/{uuid4().hex}", "a": p.people["PC"], "at": at, "v": view_code})


def test_physical_delivery_verification_is_read_from_the_vehicle_photos(project):
    p = project
    _on(p)
    _duty(p, title="Physical delivery verification", closeMode="AUTO", checkerCode="VEHICLE_PHOTOS")
    title = "Physical delivery verification"
    # Nothing in Verigence for the day: the PC's to confirm.
    nothing = _one(_day(p), title)
    assert nothing["progress"]["state"] == "NOTHING" and nothing["canTick"]
    assert nothing["nilLabel"] == "No deliveries on this day"
    first = _delivery(p, MON)
    second = _delivery(p, MON, photo_at=_at(MON, 12))
    _delivery(p, SAT)  # another day's delivery is not Monday's
    waiting = _one(_day(p, hh=13), title)
    assert waiting["status"] == "PENDING" and not waiting["canTick"]
    assert waiting["progress"] == {"state": "PENDING", "note": "Photos on 1 of 2 deliveries", "done": 1, "total": 2}
    with pytest.raises(dd.DutyConflict, match="ticks itself"):
        _tick(p, waiting, hh=13)
    with pytest.raises(dd.DutyError, match="Give a reason"):
        _close(p)
    _photo(p, first, _at(MON, 14), view_code="EXTRA")  # an Extras page is not a photo of the car
    assert _one(_day(p, hh=15), title)["progress"]["done"] == 1
    _photo(p, first, _at(MON, 16))
    # Done so far, while Monday still runs: on track, no reason needed, not ticked yet.
    on_track = _day(p, hh=17)
    assert _one(on_track, title)["status"] == "ON_TRACK" and on_track["counts"]["needReason"] == 0
    assert _close(p)["dayClosedAt"] is not None
    assert _team(p)["days"][0]["outlets"][0]["items"][0]["state"] == "ON_TRACK"
    # A photo taken on the day counts a Journey as that day's delivery even with no gate pass yet.
    _photo(p, second, _at(MON, 18))
    # Once Monday is over the system ticks it, dated by the last photo: done on Monday.
    team = _team(p, start=MON, end=TUE)
    monday = next(d for d in team["days"] if d["date"] == MON.isoformat())
    cell = next(i for i in monday["outlets"][0]["items"] if i["title"] == title)
    assert (cell["state"], cell["doneSource"], cell["comment"]) == ("DONE", "SYSTEM", "Photos on 2 of 2 deliveries")


def test_the_pc_confirms_a_day_with_no_deliveries(project):
    p = project
    _on(p)
    _duty(p, title="Photos", closeMode="AUTO", checkerCode="VEHICLE_PHOTOS")
    done = _one(_tick(p, _one(_day(p), "Photos"), comment="No deliveries today"), "Photos")
    assert (done["status"], done["doneSource"]) == ("DONE", "PC")


def test_uploads_are_read_against_the_files_received(project):
    p = project
    with _Db(p) as c:
        dd.add_standard_duties(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"), now=datetime.now(UTC))
        dd.update_settings(c, tenant_id=p.tenant, caller=_caller(p, "EXECUTIVE"),
                           spec={"enabled": True, "workingWeekdays": [1, 2, 3, 4, 5, 6, 7]}, now=datetime.now(UTC))
        view = dd.my_day(c, tenant_id=p.tenant, caller=_caller(p, "PC"))
    uploads = _one(view, "Upload bookings and deliveries")
    assert uploads["progress"] == {"state": "PENDING", "note": "Enter the files received first", "done": None,
                                   "total": None}
    received = _one(view, "Booking and delivery files received")
    with _Db(p) as c:
        view = dd.tick(c, tenant_id=p.tenant, caller=_caller(p, "PC"), item_id=UUID(received["itemId"]),
                       values={"bookingFiles": 1, "deliveryFiles": 1}, comment=None)
    assert _one(view, "Upload bookings and deliveries")["progress"] == {
        "state": "PENDING", "note": "Bookings 0 of 1, deliveries 0 of 1 uploaded", "done": 0, "total": 2}
    add_batch_pages(p.journey, [("booking_form", "READY")])  # a booking uploaded today
    with _Db(p) as c:
        view = dd.my_day(c, tenant_id=p.tenant, caller=_caller(p, "PC"))
    assert _one(view, "Upload bookings and deliveries")["progress"]["note"] == (
        "Bookings 1 of 1, deliveries 0 of 1 uploaded")
    other = _delivery(p, dd.ist_today())
    delivered = SimpleNamespace(**{**p.journey.__dict__, "journey_id": other})
    add_batch_pages(delivered, [("delivery_order", "READY")])
    with _Db(p) as c:
        c.execute(text("UPDATE auditcore.p2_document_queue SET business_stage = 'DELIVERY' "
                       "WHERE tenant_id = :t AND journey_id = :j"), {"t": p.tenant, "j": other})
        view = dd.my_day(c, tenant_id=p.tenant, caller=_caller(p, "PC"))
    done = _one(view, "Upload bookings and deliveries")
    assert done["status"] == "ON_TRACK" and done["progress"]["note"] == "Bookings 1 of 1, deliveries 1 of 1 uploaded"


def test_the_bank_statement_ticks_itself_for_the_outlet_or_its_dealership(project):
    p = project
    _on(p)
    _duty(p, title="Bank statement", closeMode="AUTO", checkerCode="BANK_STATEMENT", rolling=True,
          allowNotReceived=True)
    item = _one(_day(p), "Bank statement")
    assert item["progress"]["note"] == "No bank statement uploaded since 23 Nov" and item["allowNotReceived"]

    def statement(at: datetime, outlet=None) -> None:
        with _Db(p) as c:
            c.execute(text("""
                INSERT INTO auditcore.p2_bank_statements (tenant_id, statement_id, dealer_id, outlet_id, period_from,
                  period_to, file_name, file_format, uploaded_by, uploaded_by_name, uploaded_at_utc)
                VALUES (:t, :s, :d, :o, :a, :a, 'statement.pdf', 'PDF', :by, 'Pc Two', :at)
            """), {"t": p.tenant, "s": uuid4(), "d": p.journey.dealer_id, "o": outlet, "a": at.date(),
                   "by": p.people["PC2"], "at": at})

    statement(_at(SAT, 12))  # before the item came on the list
    assert _one(_day(p), "Bank statement")["status"] == "PENDING"
    statement(_at(TUE, 12))  # the dealership's statement, uploaded by the other PC on Tuesday
    tuesday = _one(_day(p, day=TUE, hh=13), "Bank statement")
    assert (tuesday["status"], tuesday["doneSource"], tuesday["doneByName"]) == ("DONE", "SYSTEM", "Pc Two")
    states = {d["date"]: [i["state"] for o in d["outlets"] for i in o["items"]]
              for d in _team(p, start=MON, end=TUE)["days"]}
    assert states == {TUE.isoformat(): ["DONE_LATE"], MON.isoformat(): ["PENDING"]}


def _task(p, *, journey=None, role: str = "PC", status: str = "READY", scope: str = "JOURNEY") -> str:
    """A task on a Journey of the outlet (the Tasks screen), as an upload raises it."""
    task_id = str(uuid4())
    with _Db(p) as c:
        c.execute(
            text("""
                INSERT INTO auditcore.p2_tasks (tenant_id, task_id, root_task_id, journey_id, task_type, category,
                  origin_kind, source_type, dedupe_key, title, description, assigned_role_code, allowed_actions,
                  completion_protocol, task_status, scope_kind)
                VALUES (:t, :id, :id, :j, 'DOCUMENT_MISSING', 'DOCUMENT', 'SYSTEM', 'RULE', :dk, 'Upload the invoice',
                  'x', :role, '[]'::jsonb, 'MACHINE_VERIFIED', :status, :scope)
            """),
            {"t": p.tenant, "id": task_id, "j": str(journey or p.journey.journey_id), "dk": f"dd:{task_id}",
             "role": role, "status": status, "scope": scope})
    return task_id


def _task_event(p, task_id: str, event: str, at: datetime, *, actor: str | None = None, role: str = "PC") -> None:
    with _Db(p) as c:
        c.execute(
            text("INSERT INTO auditcore.p2_task_events (tenant_id, task_id, event_type, actor_id, actor_role_code, "
                 "created_at_utc) VALUES (:t, :task, :e, :a, :r, :at)"),
            {"t": p.tenant, "task": task_id, "e": event, "a": actor or p.people["PC"], "r": role, "at": at})
        if event == "CLOSE_TASK":
            c.execute(text("UPDATE auditcore.p2_tasks SET task_status = 'VERIFIED_COMPLETE' "
                           "WHERE tenant_id = :t AND task_id = :id"), {"t": p.tenant, "id": task_id})


def test_working_on_the_open_tasks_is_read_from_the_tasks(project):
    """Product owner, 19 Nov 2026: "one duty of working on the Tasks which has been
    accumulated or raised as we upload the files"."""
    p = project
    title = "Work on open tasks"
    _on(p)
    _duty(p, title=title, closeMode="AUTO", checkerCode="OPEN_TASKS", carryDays=0)
    # No task on the outlet's files: nothing to judge by, the PC confirms.
    nothing = _one(_day(p), title)
    assert nothing["progress"]["state"] == "NOTHING" and nothing["canTick"]
    assert nothing["nilLabel"] == "No open tasks"
    first, second, third = _task(p), _task(p), _task(p)
    _task(p, role="TL")  # a Team Lead's task is not the PC's backlog
    waiting = _one(_day(p, hh=12), title)
    assert waiting["status"] == "PENDING" and not waiting["canTick"]
    assert waiting["progress"]["note"] == "3 open tasks, none worked on today"
    with pytest.raises(dd.DutyError, match="Give a reason"):
        _close(p)
    # What the system does on its own, and what a Team Lead does, is not the PC working on tasks.
    _task_event(p, first, "COMPLETED", _at(MON, 12, 30), actor="SYSTEM", role="SYSTEM")
    _task_event(p, first, "ADD_COMMENT", _at(MON, 12, 40), actor=p.people["TL"], role="TL")
    _task_event(p, first, "CREATED", _at(MON, 12, 45))  # not an action on the task
    assert _one(_day(p, hh=13), title)["status"] == "PENDING"
    # The PC works on two tasks and closes one: on track while Monday runs, no reason needed.
    _task_event(p, first, "ADD_COMMENT", _at(MON, 14))
    _task_event(p, second, "UPLOAD_DOCUMENT", _at(MON, 15))
    _task_event(p, second, "CLOSE_TASK", _at(MON, 15, 5))
    on_track = _day(p, hh=16)
    item = _one(on_track, title)
    assert item["status"] == "ON_TRACK" and on_track["counts"]["needReason"] == 0
    assert item["progress"]["note"] == "Worked on 2 tasks, 1 closed, 2 still open"
    assert _close(p)["dayClosedAt"] is not None
    # Tuesday: Monday is ticked with Monday's numbers; Tuesday starts again with what is open.
    tuesday = _day(p, day=TUE, hh=10)
    assert [i["dutyDate"] for i in _items(tuesday) if i["title"] == title] == [TUE.isoformat()]
    assert _one(tuesday, title)["progress"]["note"] == "2 open tasks, none worked on today"
    team = _team(p, start=MON, end=WED, day=WED)
    cells = {d["date"]: next(i for i in d["outlets"][0]["items"] if i["title"] == title) for d in team["days"]}
    assert (cells[MON.isoformat()]["state"], cells[MON.isoformat()]["comment"]) == (
        "DONE", "Worked on 2 tasks, 1 closed")
    # Nothing was worked on Tuesday and it is not carried: Tuesday is incomplete.
    assert cells[TUE.isoformat()]["state"] == "INCOMPLETE"
    assert third  # still open, never touched


def test_a_checker_that_fails_never_takes_the_list_down(project, monkeypatch):
    p = project
    _on(p)
    _duty(p, title="Gate out register", dayOffset=-1, closeMode="AUTO", checkerCode="GATE_REGISTER")
    _duty(p, title="Stock")

    def broken(connection, tenant_id, items, today):
        connection.execute(text("SELECT 1 FROM auditcore.no_such_table"))

    monkeypatch.setitem(daily_duty_checkers.CHECKERS["GATE_REGISTER"], "check", broken)
    view = _day(p)
    register = _one(view, "Gate out register")
    assert register["status"] == "PENDING" and register["checkFailed"] and register["progress"] is None
    assert _one(_tick(p, _one(view, "Stock")), "Stock")["status"] == "DONE"  # the transaction is still good


# --------------------------------------------------------------------------- the team's view


def test_a_team_lead_sees_only_the_outlets_under_them(project):
    p = project
    _on(p)
    _duty(p)
    with p.engine.begin() as c:
        other = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, 'X2', 'Other') RETURNING outlet_id"),
            {"t": p.tenant, "d": p.journey.dealer_id}).scalar_one()
        c.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id, outlet_id) VALUES (:t, 'dd-pc3', 'PC', :d, :o), (:t, 'dd-tl2', 'TL', :d, :o)"),
            {"t": p.tenant, "d": p.journey.dealer_id, "o": other})
    names = lambda view: sorted(o["outletName"] for o in view["days"][0]["outlets"])
    assert names(_team(p, "PM")) == ["O", "Other"]
    assert names(_team(p, "TL")) == ["O", "Other"]  # a Project-wide Team Lead
    with _Db(p) as c:
        scoped = dd.team_view(c, tenant_id=p.tenant, caller=Caller(actor_id="dd-tl2", role="TL"), start=MON,
                              end=MON, now=_at(MON, 19))
    assert names(scoped) == ["Other"]
    with pytest.raises(dd.DutyForbidden), _Db(p) as c:
        dd.team_view(c, tenant_id=p.tenant, caller=_caller(p, "PC"), start=MON, end=MON, now=_at(MON, 19))
    with pytest.raises(dd.DutyError, match="at most 31 days"), _Db(p) as c:
        dd.team_view(c, tenant_id=p.tenant, caller=_caller(p, "PM"), start=MON - timedelta(days=40), end=MON,
                     now=_at(MON, 19))


def test_day_close_states_of_the_pcs(project):
    p = project
    _on(p, day=SAT)
    _duty(p, day=SAT, weekdays=[1, 2, 3, 4, 5, 6, 7])
    _close(p, [{"itemId": _one(_day(p, day=SAT))["itemId"], "category": "SYSTEM_ISSUE"}], day=SAT)
    team = _team(p, start=SAT, end=MON, hh=12)  # Monday noon, before the cut-off
    states = {d["date"]: {pc["actorId"]: pc["dayClose"] for o in d["outlets"] for pc in o["pcs"]}
              for d in team["days"]}
    assert states[SAT.isoformat()] == {p.people["PC"]: "CLOSED", p.people["PC2"]: "NOT_CLOSED"}
    assert set(states[SUN.isoformat()].values()) == {"OFF"}  # not a working day
    assert set(states[MON.isoformat()].values()) == {"OPEN"}
    assert set(_team(p, start=MON, end=MON, hh=21)["days"][0]["outlets"][0]["pcs"][0]["dayClose"].split()) == {
        "NOT_CLOSED"}
    by_pc = {x["actorId"]: (x["daysClosed"], x["daysNotClosed"]) for x in team["byPc"]}
    assert by_pc == {p.people["PC"]: (1, 0), p.people["PC2"]: (0, 1)}


# --------------------------------------------------------------------------- HTTP


def test_http_routes_and_role_gates(project):
    p = project
    acting = {"subject": p.people["EXECUTIVE"]}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=acting["subject"])
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{p.tenant}/duties"

    assert client.get(f"{base}/settings").json()["enabled"] is False
    assert client.put(f"{base}/settings", json={"enabled": True}).json()["enabled"] is True
    assert client.post(f"{base}/definitions/standard").json() == {"added": 7}
    made = client.post(f"{base}/definitions", json={
        "title": "Count the stock", "inputs": [{"label": "Vehicles", "type": "NUMBER"}], "allowNotReceived": True})
    assert made.status_code == 201
    duty_id = made.json()["dutyId"]
    assert client.patch(f"{base}/definitions/{duty_id}", json={"sortOrder": 5}).json()["sortOrder"] == 5
    assert len(client.get(f"{base}/definitions").json()["items"]) == 8

    acting["subject"] = p.people["TL"]
    assert client.put(f"{base}/settings", json={"enabled": False}).status_code == 403
    assert client.post(f"{base}/definitions", json={"title": "x"}).status_code == 403
    assert client.get(f"{base}/team").status_code == 200
    assert client.get(f"{base}/my-day").json()["applies"] is False

    acting["subject"] = p.people["PC"]
    assert client.get(f"{base}/team").status_code == 403
    assert client.get(f"{base}/definitions").status_code == 403
    view = client.get(f"{base}/my-day").json()
    assert view["applies"] is True
    stock = _one(view)
    assert client.post(f"{base}/items/{stock['itemId']}/tick", json={"values": {}}).status_code == 422
    assert client.post(f"{base}/items/{stock['itemId']}/not-received", json={}).status_code == 422
    marked = client.post(f"{base}/items/{stock['itemId']}/not-received", json={"informedGroup": True})
    assert marked.status_code == 200 and _one(marked.json())["status"] == "NOT_RECEIVED"
    ticked = client.post(f"{base}/items/{stock['itemId']}/tick", json={"values": {"vehicles": 12}})
    assert _one(ticked.json())["values"] == {"vehicles": 12}
    # Taken back, it is as it was before the tick: not received today.
    assert _one(client.post(f"{base}/items/{stock['itemId']}/undo").json())["status"] == "NOT_RECEIVED"
    assert client.post(f"{base}/items/{uuid4()}/tick", json={}).status_code == 404
    assert client.post(f"{base}/my-day/close", json={"reasons": []}).status_code == 422
    reasons = [{"itemId": i["itemId"], "category": "NOT_GIVEN_BY_DEALERSHIP"}
               for i in _items(client.get(f"{base}/my-day").json()) if i["status"] in {"PENDING", "NOT_RECEIVED"}]
    closed = client.post(f"{base}/my-day/close", json={"reasons": reasons})
    assert closed.status_code == 200 and closed.json()["dayClosedAt"]

    # SuperAdmin from its own view, one Project at a time.
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id="super-admin-1", bearer_token="t",
        admin_context=SecurityAdminContext(user_id="super-admin-1", is_super_admin=True, admin_scopes=()))
    admin = f"/v1/admin/tenants/{p.tenant}/duties"
    assert client.get(f"{admin}/team").json()["enabled"] is True
    assert len(client.get(f"{admin}/definitions").json()["items"]) == 8
    assert client.post(f"{admin}/definitions", json={"title": "Audit"}).status_code == 201
    assert client.patch(f"{admin}/definitions/{duty_id}", json={"status": "INACTIVE"}).json()["status"] == "INACTIVE"
    assert client.post(f"{admin}/definitions/standard").json() == {"added": 0}
    assert client.put(f"{admin}/settings", json={"enabled": False}).json()["enabled"] is False
    assert client.get(f"{base}/my-day").json()["enabled"] is False
    assert client.get("/v1/admin/tenants/no-such-project/duties/settings").status_code == 404
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id="x", bearer_token="t",
        admin_context=SecurityAdminContext(user_id="x", is_super_admin=False, admin_scopes=()))
    assert client.get(f"{admin}/team").status_code == 403
