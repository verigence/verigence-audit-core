"""Daily Gate Pass: the photo is checked and kept, the counts are checked against
the Delivery files of the day, and an unexplained gap becomes a Daily Task for
the Team Lead (real Postgres, storage doubled)."""
from __future__ import annotations

import io
import random
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_batch_pages,
    create_p2_journey,
    database_engine,
)
from PIL import Image, ImageDraw, ImageFilter
from sqlalchemy import text

from audit_core import uc03_p2_gate_pass as gate_pass
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client


class Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, key, data, *, content_type):
        self.objects[key] = data

    def presign_get(self, key, *, expires_seconds=1800):
        return f"https://storage.test/get/{key}"


def _page(width=4000, height=3000, blur=0):
    random.seed(7)
    image = Image.new("RGB", (width, height), (238, 236, 228))
    draw = ImageDraw.Draw(image)
    for y in range(150, height, 140):
        draw.line([(80, y), (width - 80, y)], fill=(170, 185, 210), width=2)
    for _ in range(500):
        x, y = random.randint(100, width - 300), random.randint(120, height - 120)
        draw.line([(x + i * 14, y + random.randint(-30, 30)) for i in range(random.randint(3, 10))],
                  fill=(25, 30, 90), width=4)
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


SHARP, BLURRED = _page(), _page(blur=6)


@pytest.fixture
def setup(monkeypatch):
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="gatepass")
    storage = Storage()
    monkeypatch.setattr(gate_pass, "get_p2_document_storage", lambda: storage)
    real_days = gate_pass.gate_pass_days
    # Sunday is the off day (no gate pass of its own); keep these same-day tests independent of when they run.
    monkeypatch.setattr(gate_pass, "gate_pass_days",
                        lambda now=None: {**real_days(now), "current": gate_pass.ist_day(now).date()})
    acting = {"subject": journey.actor_id}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=acting["subject"])
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield SimpleNamespace(journey=journey, storage=storage, client=client, acting=acting,
                              base=f"/p2/v1/tenants/{journey.tenant_id}/gate-passes")
    finally:
        app.dependency_overrides.clear()
        with engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            connection.execute(text("DELETE FROM auditcore.p2_task_events WHERE tenant_id=:t AND journey_id IS NULL"),
                               {"t": journey.tenant_id})
            connection.execute(text("DELETE FROM auditcore.p2_tasks WHERE tenant_id=:t AND scope_kind <> 'JOURNEY'"),
                               {"t": journey.tenant_id})
            for table in ("p2_daily_tasks", "p2_gate_passes"):
                connection.execute(text(f"DELETE FROM auditcore.{table} WHERE tenant_id=:t"), {"t": journey.tenant_id})
        engine.dispose()


def _upload(s, data=SHARP, name="Pc One"):
    return s.client.post(s.base, data={"outletId": str(s.journey.outlet_id), "pcName": name},
                         files={"file": ("gatepass.jpg", data, "image/jpeg")})


def _delivery_file(s, journeys=1, *, stage="DELIVERY", pages=1, fresh=False):
    """``journeys`` journeys of the outlet, each with ``pages`` pages of ``stage`` uploaded today."""
    made = []
    for n in range(journeys):
        if n == 0 and not fresh:
            j = s.journey
        else:
            with s.journey.engine.begin() as connection:
                set_tenant_context(connection, s.journey.tenant_id)
                customer = connection.execute(
                    text("SELECT customer_id FROM auditcore.customers WHERE tenant_id=:t LIMIT 1"),
                    {"t": s.journey.tenant_id}).scalar_one()
                new_id = connection.execute(
                    text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference) "
                         "VALUES (:t, :d, :o, :c, :r) RETURNING journey_id"),
                    {"t": s.journey.tenant_id, "d": s.journey.dealer_id, "o": s.journey.outlet_id, "c": customer,
                     "r": f"GP-{uuid4().hex[:8]}"}).scalar_one()
            j = SimpleNamespace(**{**s.journey.__dict__, "journey_id": UUID(str(new_id))})
        _, rows = add_batch_pages(j, [("delivery_order", "READY")] * pages)
        with s.journey.engine.begin() as connection:
            set_tenant_context(connection, s.journey.tenant_id)
            connection.execute(
                text("UPDATE auditcore.p2_document_queue SET business_stage=:s WHERE tenant_id=:t AND journey_id=:j"),
                {"s": stage, "t": s.journey.tenant_id, "j": j.journey_id})
        made.append(rows)
    return made


def _counts(s, gate_pass_id, deliveries, *, transfers=0, drives=0, other=0, note=None):
    return s.client.post(f"{s.base}/{gate_pass_id}/counts", json={
        "newDeliveries": deliveries, "stockTransfers": transfers, "testDrives": drives,
        "otherCount": other, "otherNote": note})


def test_the_onsite_outlet_is_the_default_and_the_photo_is_checked_and_named(setup):
    s = setup
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("UPDATE auditcore.dealer_outlets SET outlet_name='Cuttack Road' WHERE outlet_id=:o"),
                           {"o": s.journey.outlet_id})
    outlets = s.client.get(f"{s.base}/outlets").json()
    assert outlets["defaultOutletId"] == str(s.journey.outlet_id)
    assert [o["outletName"] for o in outlets["outlets"]] == ["Cuttack Road"]

    # No hard rules at the camera (2026-10-27): a blurred or small photo is kept and flagged.
    blurred = _upload(s, BLURRED)
    assert blurred.status_code == 201 and blurred.json()["photoQuality"] == "BLURRED"
    small = _upload(s, _page(1000, 750))
    assert small.status_code == 201
    assert s.client.post(s.base, data={"outletId": str(s.journey.outlet_id)},
                         files={"file": ("x.jpg", b"not a photo", "image/jpeg")}).status_code == 422
    s.storage.objects.clear()
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("DELETE FROM auditcore.p2_gate_passes WHERE tenant_id=:t"), {"t": s.journey.tenant_id})

    created = _upload(s)
    assert created.json()["photoQuality"] == "GOOD"
    assert created.status_code == 201, created.text
    [key] = s.storage.objects
    name = key.rsplit("/", 1)[-1]
    from audit_core.uc03_p2_object_keys import ist_day
    assert name == f"Cuttack-Road__Pc-One__{ist_day():%d-%m-%y}.jpg"
    assert key.startswith(f"daily-gate-passes/{s.journey.tenant_id}/")

    again = _upload(s)  # the same day: the new photo replaces the old, both are kept
    assert again.status_code == 201
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        statuses = [r[0] for r in connection.execute(
            text("SELECT status FROM auditcore.p2_gate_passes WHERE tenant_id=:t ORDER BY created_at_utc"),
            {"t": s.journey.tenant_id})]
    assert statuses == ["SUPERSEDED", "ACTIVE"] and len(s.storage.objects) == 2


def test_equal_counts_finish_the_task_and_bookings_are_not_counted(setup):
    s = setup
    _delivery_file(s, journeys=2, pages=3)           # two journeys, three delivery pages each: counts 2
    _delivery_file(s, journeys=1, stage="BOOKING", fresh=True)   # a booking is never a delivery file
    gp = _upload(s).json()["gatePassId"]
    result = _counts(s, gp, 2, transfers=1, drives=3).json()
    assert result["divergence"] == "NONE" and result["deliveryFiles"] == 2 and result["nextStep"] == "DONE"
    assert result["gatePass"]["completed"] is True
    assert _counts(s, gp, 2).status_code == 409   # finished


def test_more_files_than_declared_asks_which_belong_to_other_days(setup):
    s = setup
    _delivery_file(s, journeys=3)
    gp = _upload(s).json()["gatePassId"]
    result = _counts(s, gp, 1).json()
    assert result["divergence"] == "EXTRA_FILES" and result["difference"] == 2
    assert result["nextStep"] == "EXPLAIN_EXTRA_FILES" and result["gatePass"]["completed"] is False
    assert s.client.post(f"{s.base}/{gp}/answer", json={}).status_code == 422
    done = s.client.post(f"{s.base}/{gp}/answer", json={"pastDeliveries": "Ramesh Das, J-1042"}).json()
    assert done["gatePass"]["completed"] is True and done["taskRaised"] is False


def test_fewer_files_than_declared_raises_a_high_priority_daily_task_only_on_no(setup):
    s = setup
    _delivery_file(s, journeys=1)
    gp = _upload(s).json()["gatePassId"]
    result = _counts(s, gp, 3).json()
    assert result["divergence"] == "MISSING_FILES" and result["difference"] == 2
    assert result["nextStep"] == "CONFIRM_FILES_HELD"
    assert s.client.post(f"{s.base}/{gp}/answer", json={}).status_code == 422

    yes = s.client.post(f"{s.base}/{gp}/answer", json={"filesHeld": True}).json()
    assert yes["taskRaised"] is False

    # a second day's gate pass (a new photo replaces today's) answered NO
    gp2 = _upload(s).json()["gatePassId"]
    _counts(s, gp2, 3)
    no = s.client.post(f"{s.base}/{gp2}/answer", json={"filesHeld": False}).json()
    assert no["taskRaised"] is True
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        [task] = connection.execute(
            text("SELECT category, task_type, priority, assigned_role_code, task_status, scope_kind, "
                 "completion_protocol FROM auditcore.p2_tasks WHERE tenant_id=:t AND category='DAILY_TASK'"),
            {"t": s.journey.tenant_id}).all()
    # Daily tasks live in the Task Manager (2026-10-26): an outlet task for the
    # Team Lead, closed by the TL with a note, no reviewer.
    assert tuple(task) == ("DAILY_TASK", "GATE_PASS_DELIVERY_NOT_NOTIFIED", "HIGH", "TL", "READY", "OUTLET",
                           "SELF_CLOSING")


def test_other_entries_need_a_description(setup):
    s = setup
    gp = _upload(s).json()["gatePassId"]
    assert _counts(s, gp, 0, other=2).status_code == 422
    assert _counts(s, gp, 0, other=2, note="Insurance pickup").status_code == 200


def test_calendar_shows_outlet_photo_but_no_photo_for_a_dealership_view_and_tl_resolves(setup):
    s = setup
    _delivery_file(s, journeys=1)
    gp = _upload(s).json()["gatePassId"]
    _counts(s, gp, 3)
    s.client.post(f"{s.base}/{gp}/answer", json={"filesHeld": False})
    from audit_core.uc03_p2_object_keys import ist_day
    month = f"{ist_day():%Y-%m}"
    today = f"{ist_day():%Y-%m-%d}"

    outlet_view = s.client.get(f"{s.base}/calendar", params={"month": month, "outletId": str(s.journey.outlet_id)}).json()
    [day] = outlet_view["days"]
    assert (day["date"], day["declared"], day["uploaded"], day["missing"]) == (today, 3, 1, 2)
    assert day["gatePassId"] == gp and outlet_view["scope"] == "OUTLET"
    assert outlet_view["totals"] == {"declared": 3, "uploaded": 1, "missing": 2}

    dealer_view = s.client.get(f"{s.base}/calendar", params={"month": month, "dealerId": str(s.journey.dealer_id)}).json()
    assert dealer_view["scope"] == "DEALER" and "gatePassId" not in dealer_view["days"][0]
    assert "gatePassId" not in s.client.get(f"{s.base}/calendar", params={"month": month}).json()["days"][0]

    url = s.client.get(f"{s.base}/{gp}/image-url").json()
    assert url["url"].startswith("https://storage.test/get/daily-gate-passes/")

    # the PC supervises nothing: no Daily Tasks; a TL of the outlet sees and resolves it
    assert s.client.get(f"{s.base.replace('gate-passes', 'daily-tasks')}").json()["items"] == []
    tl = f"gatepass-tl-{uuid4().hex[:8]}"
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id) VALUES (:t, :a, 'TL', :d)"),
            {"t": s.journey.tenant_id, "a": tl, "d": s.journey.dealer_id})
    s.acting["subject"] = tl
    tasks_url = s.base.replace("gate-passes", "daily-tasks")
    [task] = s.client.get(tasks_url).json()["items"]
    assert task["priority"] == "HIGH" and task["category"] == "DAILY_TASK" and task["status"] == "OPEN"
    assert s.client.post(f"{tasks_url}/{task['dailyTaskId']}/resolve", json={"note": ""}).status_code == 400
    done = s.client.post(f"{tasks_url}/{task['dailyTaskId']}/resolve", json={"note": "Called the dealer"}).json()
    assert done["status"] == "DONE"
    assert s.client.get(tasks_url).json()["items"] == []


def test_a_late_gate_pass_is_filed_under_its_own_day_and_the_screen_asks(setup, monkeypatch):
    """Product owner 2026-10-27: the previous day's gate pass can still be
    uploaded the next day; the screen asks which day the photo is for."""
    from datetime import date, timedelta

    s = setup
    tuesday, monday = date(2026, 10, 27), date(2026, 10, 26)
    monkeypatch.setattr(gate_pass, "gate_pass_days",
                        lambda now=None: {"today": tuesday, "current": tuesday, "previous": monday})
    outlets = s.client.get(f"{s.base}/outlets").json()
    assert outlets["today"]["label"] == "Tue 27 Oct"
    assert outlets["previous"] == {"date": "2026-10-26", "label": "Sun 25 + Mon 26 Oct",
                                   "coversDates": ["2026-10-25", "2026-10-26"],
                                   "dueAtUtc": "2026-10-27T18:29:59+00:00"}
    assert outlets["previousOpenOutletIds"] == [str(s.journey.outlet_id)]

    late = s.client.post(s.base, data={"outletId": str(s.journey.outlet_id), "businessDate": "2026-10-26"},
                         files={"file": ("gatepass.jpg", SHARP, "image/jpeg")})
    assert late.status_code == 201, late.text
    assert late.json()["businessDate"] == "2026-10-26" and late.json()["dayLabel"] == "Sun 25 + Mon 26 Oct"
    too_old = s.client.post(s.base, data={"outletId": str(s.journey.outlet_id),
                                          "businessDate": (monday - timedelta(days=2)).isoformat()},
                            files={"file": ("gatepass.jpg", SHARP, "image/jpeg")})
    assert too_old.status_code == 422 and "Tue 27 Oct or Sun 25 + Mon 26 Oct" in too_old.json()["detail"]


def test_sunday_goes_with_monday(setup, monkeypatch):
    from datetime import date

    s = setup
    sunday = date(2026, 11, 1)
    monkeypatch.setattr(gate_pass, "gate_pass_days",
                        lambda now=None: {"today": sunday, "current": None, "previous": date(2026, 10, 31)})
    refused = s.client.post(s.base, data={"outletId": str(s.journey.outlet_id), "businessDate": "2026-11-01"},
                            files={"file": ("gatepass.jpg", SHARP, "image/jpeg")})
    assert refused.status_code == 422 and "Monday" in refused.json()["detail"]
    saturday = s.client.post(s.base, data={"outletId": str(s.journey.outlet_id)},
                             files={"file": ("gatepass.jpg", SHARP, "image/jpeg")})
    assert saturday.status_code == 201 and saturday.json()["businessDate"] == "2026-10-31"


def test_consolidated_view_register_tool_and_pending(setup):
    """Product owner 2026-10-27: TL / PM / Executive see, per onsite outlet, as per
    gate-out register, updated in tool, pending due to client and due to PC."""
    s = setup
    _delivery_file(s, journeys=2)
    gp = _upload(s).json()["gatePassId"]
    assert _counts(s, gp, 5).json()["divergence"] == "MISSING_FILES"
    view = s.client.get(f"{s.base}/summary").json()
    [row] = view["outlets"]
    assert (row["asPerRegister"], row["updatedInTool"], row["pendingDueToPc"], row["pendingDueToClient"]) == (5, 2, 3, 0)
    assert row["days"][-1]["status"] == "ANSWER_PENDING" and row["days"][-1]["photoQuality"] == "GOOD"
    assert s.client.post(f"{s.base}/{gp}/answer", json={"filesHeld": False}).status_code == 200
    after = s.client.get(f"{s.base}/summary").json()
    assert (after["totals"]["pendingDueToClient"], after["totals"]["pendingDueToPc"]) == (3, 0)
    assert after["totals"]["gatePassesFinished"] == 1
    _delivery_file(s, journeys=1, fresh=True)  # one of them uploaded later the same day
    assert s.client.get(f"{s.base}/summary").json()["totals"]["pendingDueToClient"] == 2


def test_satellite_outlets_have_no_gate_pass(setup):
    s = setup
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("UPDATE auditcore.dealer_outlets SET outlet_classification='SATELLITE' "
                                "WHERE outlet_id=:o"), {"o": s.journey.outlet_id})
    outlets = s.client.get(f"{s.base}/outlets").json()
    assert outlets["outlets"] == [] and outlets["satelliteOnly"] is True
    refused = _upload(s)
    assert refused.status_code == 422 and "onsite" in refused.json()["detail"]
    assert s.client.get(f"{s.base}/summary").json()["outlets"] == []
