"""Daily Gate Pass: the photo is checked and kept, the counts are checked against
the Delivery files of the day, and an unexplained gap becomes a Daily Task for
the Team Lead (real Postgres, storage doubled)."""
from __future__ import annotations

import io
import json
import random
from datetime import UTC, date, datetime, time, timedelta
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
REAL_WINDOW = gate_pass.gate_pass_window


def _window_on(monkeypatch, day: date) -> None:
    """The dates a PC can choose as on ``day`` (10:00 India time)."""
    at = datetime.combine(day, time(10, 0), tzinfo=gate_pass.ist_day().tzinfo).astimezone(UTC)
    monkeypatch.setattr(gate_pass, "gate_pass_window", lambda now=None: REAL_WINDOW(at))


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

    def window(now=None):
        w = REAL_WINDOW(now)
        return w if w["today"] in w["days"] else {**w, "days": [w["today"], *w["days"]]}
    monkeypatch.setattr(gate_pass, "gate_pass_window", window)
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
            for table in ("p2_daily_tasks", "p2_gate_pass_pages", "p2_gate_passes"):
                connection.execute(text(f"DELETE FROM auditcore.{table} WHERE tenant_id=:t"), {"t": journey.tenant_id})
        engine.dispose()


def _today() -> str:
    return gate_pass.ist_day().date().isoformat()


def _upload(s, data=SHARP, name="Pc One", day=None):
    return s.client.post(s.base, data={"outletId": str(s.journey.outlet_id), "pcName": name,
                                       "businessDate": day or _today()},
                         files={"file": ("gatepass.jpg", data, "image/jpeg")})


_TODAY = object()


def _vehicle_gate_pass(s, journey_id, read_date, *, corrected_to=None):
    """The vehicle's own gate pass on a Journey (a live document), with the delivery date
    read off it (and a reviewer's correction, when given)."""
    document = uuid4()
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id, di_document_id, "
                 "document_type_key, evidence_purpose, process_area, association_status) "
                 "SELECT tenant_id, journey_id, customer_id, :s, :d, 'gate_pass', 'JOURNEY_DOCUMENT', 'DELIVERY', "
                 "'ACTIVE' FROM auditcore.journeys WHERE tenant_id = :t AND journey_id = :j"),
            {"t": s.journey.tenant_id, "j": str(journey_id), "s": str(uuid4()), "d": str(document)})
        connection.execute(
            text("INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id, di_document_id, "
                 "field_key, stage_code, source_document_type_key, extracted_value, modified_value, "
                 "modified_by_actor_id, modified_at_utc, effective_value, is_modified) "
                 "VALUES (:t, :j, :d, 'delivery_date', 'DELIVERY', 'gate_pass', CAST(:x AS jsonb), CAST(:m AS jsonb), "
                 "CAST(:a AS varchar), CASE WHEN CAST(:a AS varchar) IS NULL THEN NULL ELSE now() END, CAST(:e AS jsonb), :im)"),
            {"t": s.journey.tenant_id, "j": str(journey_id), "d": str(document),
             "x": json.dumps(str(read_date)), "m": json.dumps(str(corrected_to)) if corrected_to else None,
             "a": s.journey.actor_id if corrected_to else None,
             "e": json.dumps(str(corrected_to or read_date)), "im": corrected_to is not None})


def _delivery_file(s, journeys=1, *, stage="DELIVERY", pages=1, fresh=False, gate_pass_dated=_TODAY):
    """``journeys`` journeys of the outlet, each with ``pages`` pages of ``stage`` uploaded today and,
    for a delivery, the vehicle's gate pass dated ``gate_pass_dated`` (today by default; None: no
    vehicle gate pass uploaded)."""
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
        if stage == "DELIVERY" and gate_pass_dated is not None:
            _vehicle_gate_pass(s, j.journey_id, gate_pass.ist_day().date() if gate_pass_dated is _TODAY else gate_pass_dated)
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
    assert s.client.post(s.base, data={"outletId": str(s.journey.outlet_id), "businessDate": _today()},
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
    # Finished -- but its PC may still correct the counts on the day it was uploaded (2026-11-03).
    assert _counts(s, gp, 2).status_code == 200


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
    # the older "files not held" answer: the whole gap was not intimated to the PC
    assert (day["notNotified"], day["toUpload"]) == (2, 0)
    assert outlet_view["totals"] == {"declared": 3, "uploaded": 1, "missing": 2, "notNotified": 2, "toUpload": 0}

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


def test_older_clients_still_get_today_and_the_previous_day(setup, monkeypatch):
    """GET /gate-passes/outlets keeps today / previous / previousOpenOutletIds for older
    clients (product owner 2026-10-27); the PC now chooses the date (2026-11-07)."""
    s = setup
    tuesday, monday = date(2026, 10, 27), date(2026, 10, 26)
    monkeypatch.setattr(gate_pass, "gate_pass_days",
                        lambda now=None: {"today": tuesday, "current": tuesday, "previous": monday})
    _window_on(monkeypatch, tuesday)
    outlets = s.client.get(f"{s.base}/outlets").json()
    assert outlets["today"]["label"] == "Tue 27 Oct"
    assert outlets["previous"] == {"date": "2026-10-26", "label": "Sun 25 + Mon 26 Oct",
                                   "coversDates": ["2026-10-25", "2026-10-26"],
                                   "dueAtUtc": "2026-10-27T18:29:59+00:00"}
    assert outlets["previousOpenOutletIds"] == [str(s.journey.outlet_id)]
    # Tue 27 back to Tue 20 Oct, Sunday 25 left out.
    assert [d["date"] for d in outlets["window"]["days"]] == [
        "2026-10-27", "2026-10-26", "2026-10-24", "2026-10-23", "2026-10-22", "2026-10-21", "2026-10-20"]
    assert (outlets["window"]["uploadFrom"], outlets["window"]["editFrom"]) == ("2026-10-20", "2026-10-24")

    late = s.client.post(s.base, data={"outletId": str(s.journey.outlet_id), "businessDate": "2026-10-26"},
                         files={"file": ("gatepass.jpg", SHARP, "image/jpeg")})
    assert late.status_code == 201, late.text
    assert late.json()["businessDate"] == "2026-10-26" and late.json()["dayLabel"] == "Sun 25 + Mon 26 Oct"


def test_the_pc_must_choose_the_date(setup):
    """Product owner 2026-11-07: the PC is asked which date the register is for; no default."""
    s = setup
    refused = s.client.post(s.base, data={"outletId": str(s.journey.outlet_id)},
                            files={"file": ("gatepass.jpg", SHARP, "image/jpeg")})
    assert refused.status_code == 422 and "Choose the date" in refused.json()["detail"]


def test_a_new_register_for_any_day_from_today_back_to_n_minus_7(setup, monkeypatch):
    s = setup
    wednesday = date(2026, 11, 4)
    _window_on(monkeypatch, wednesday)
    # N-7 (Wed 28 Oct) to N, Sunday 1 Nov left out: each can take a new register.
    for back in (0, 1, 2, 4, 5, 6, 7):
        day = wednesday - timedelta(days=back)
        made = _upload(s, day=day.isoformat())
        assert made.status_code == 201, (day, made.text)
        assert made.json()["businessDate"] == day.isoformat()
    locked = _upload(s, day=(wednesday - timedelta(days=4)).isoformat())
    assert locked.status_code == 409 and locked.json()["detail"] == (
        "The gate register for Sat 31 Oct is locked: a register already uploaded can be changed only up "
        "to 3 days after its date.")
    too_old = _upload(s, day=(wednesday - timedelta(days=8)).isoformat())
    assert too_old.status_code == 422
    assert too_old.json()["detail"] == ("A gate register can be uploaded only for Wed 28 Oct to Wed 4 Nov "
                                        "(today back to 7 days ago).")
    assert _upload(s, day=(wednesday + timedelta(days=1)).isoformat()).status_code == 422   # not come yet
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        assert connection.execute(text("SELECT count(*) FROM auditcore.p2_gate_passes WHERE tenant_id=:t "
                                       "AND status='ACTIVE'"), {"t": s.journey.tenant_id}).scalar_one() == 7


def test_sunday_goes_with_monday(setup, monkeypatch):
    s = setup
    sunday = date(2026, 11, 1)
    _window_on(monkeypatch, sunday)
    refused = _upload(s, day="2026-11-01")
    assert refused.status_code == 422 and "Monday" in refused.json()["detail"]
    # Last Sunday, within the 7 days, is not a date of its own either.
    assert "Monday" in _upload(s, day="2026-10-25").json()["detail"]
    saturday = _upload(s, day="2026-10-31")
    assert saturday.status_code == 201 and saturday.json()["businessDate"] == "2026-10-31"
    days = s.client.get(f"{s.base}/days", params={"outletId": str(s.journey.outlet_id)}).json()
    assert "2026-11-01" not in [d["date"] for d in days["days"]] and "2026-10-25" not in [d["date"] for d in days["days"]]
    # On Monday, Monday's register covers the Sunday.
    _window_on(monkeypatch, date(2026, 11, 2))
    monday = _upload(s, day="2026-11-02").json()
    assert monday["coversDates"] == ["2026-11-01", "2026-11-02"]


def test_the_days_say_new_editable_or_locked(setup, monkeypatch):
    s = setup
    wednesday = date(2026, 11, 4)
    _window_on(monkeypatch, wednesday)
    for day in ("2026-11-04", "2026-11-02", "2026-10-30"):
        assert _upload(s, day=day).status_code == 201
    days = s.client.get(f"{s.base}/days", params={"outletId": str(s.journey.outlet_id)}).json()
    by_date = {d["date"]: d for d in days["days"]}
    assert list(by_date) == ["2026-11-04", "2026-11-03", "2026-11-02", "2026-10-31", "2026-10-30", "2026-10-29",
                             "2026-10-28"]
    assert (days["today"], days["uploadFrom"], days["editFrom"]) == ("2026-11-04", "2026-10-28", "2026-11-01")
    assert by_date["2026-11-04"]["state"] == "EDITABLE" and by_date["2026-11-04"]["isToday"] is True
    assert by_date["2026-11-03"]["state"] == "NEW" and by_date["2026-11-03"]["register"] is None
    assert by_date["2026-11-02"]["state"] == "EDITABLE" and by_date["2026-11-02"]["label"] == "Sun 1 + Mon 2 Nov"
    assert by_date["2026-11-02"]["register"]["photosEditable"] is True
    assert by_date["2026-11-02"]["register"]["editableUntil"] == "2026-11-05"
    locked = by_date["2026-10-30"]
    assert (locked["state"], locked["canUpload"], locked["canEdit"]) == ("LOCKED", False, False)
    assert locked["register"]["photosEditable"] is False and "locked" in locked["note"]
    assert by_date["2026-10-29"]["state"] == "NEW" and by_date["2026-10-29"]["canUpload"] is True


def _add(s, gp, n=1, page_date=None):
    data = {"pageDate": page_date} if page_date else {}
    return s.client.post(f"{s.base}/{gp}/pages", data=data,
                         files=[("files", (f"a{i}.jpg", SHARP, "image/jpeg")) for i in range(n)])


def test_photos_are_added_replaced_and_removed_up_to_n_minus_3(setup, monkeypatch):
    s = setup
    wednesday = date(2026, 11, 4)
    _window_on(monkeypatch, wednesday)
    gp = _upload(s, day="2026-11-02").json()["gatePassId"]   # Monday 2 Nov: N-2
    assert _register(s, gp, 2, 2).status_code == 200
    added = _add(s, gp, 2)
    assert added.status_code == 201, added.text
    body = added.json()
    assert [p["position"] for p in body["pages"]] == [1, 2, 3] and [p["pageNo"] for p in body["pages"]] == [1, 2, 3]
    assert body["newDeliveries"] == 2 and body["completed"] is True and body["pagesEditCount"] == 1  # counts kept
    # MAX_PAGES: four photos at most.
    over = _add(s, gp, 2)
    assert over.status_code == 422 and "at most 4 photos and this one has 3" in over.json()["detail"]
    assert _add(s, gp, 1).status_code == 201
    assert "Remove a photo first" in _add(s, gp, 1).json()["detail"]
    # Remove photo 2: kept, marked removed; the rest renumber by position, not by id.
    removed = s.client.delete(f"{s.base}/{gp}/pages/2")
    assert removed.status_code == 200, removed.text
    pages = removed.json()["pages"]
    assert [p["pageNo"] for p in pages] == [1, 3, 4] and [p["position"] for p in pages] == [1, 2, 3]
    assert s.client.delete(f"{s.base}/{gp}/pages/2").status_code == 404
    assert s.client.get(f"{s.base}/{gp}/image-url", params={"page": 2}).json()["filename"].endswith("__p3.jpg")
    assert s.client.get(f"{s.base}/{gp}/image-url", params={"page": 4}).status_code == 404
    added_again = _add(s, gp, 1).json()
    assert [p["pageNo"] for p in added_again["pages"]] == [1, 3, 4, 5]
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        kept = connection.execute(text("SELECT page_no, removed_by_actor_id FROM auditcore.p2_gate_pass_pages "
                                       "WHERE tenant_id=:t AND gate_pass_id=:g AND removed_at_utc IS NOT NULL"),
                                  {"t": s.journey.tenant_id, "g": gp}).all()
    assert [tuple(r) for r in kept] == [(2, s.journey.actor_id)]
    assert len(s.storage.objects) == 5     # nothing is deleted from storage
    # The last photo stays.
    for page_no in (1, 3, 4):
        assert s.client.delete(f"{s.base}/{gp}/pages/{page_no}").status_code == 200
    last = s.client.delete(f"{s.base}/{gp}/pages/5")
    assert last.status_code == 409 and "at least one photo" in last.json()["detail"]
    summary = s.client.get(f"{s.base}/summary", params={"from": "2026-11-02", "to": "2026-11-02"}).json()
    assert summary["outlets"][0]["days"][0]["pages"] == 1
    # Replace all: a new register for the date; the old one is kept, superseded.
    replaced = _upload(s, day="2026-11-02")
    assert replaced.status_code == 201 and replaced.json()["gatePassId"] != gp
    assert _add(s, gp, 1).status_code == 409          # the old register is no longer the date's


def test_a_register_older_than_n_minus_3_is_locked(setup, monkeypatch):
    s = setup
    saturday = date(2026, 10, 31)
    _window_on(monkeypatch, saturday)
    gp = _upload(s, day="2026-10-31").json()["gatePassId"]
    two = _add(s, gp, 1).json()
    # Four days later (Wed 4 Nov) the Saturday register is N-4: locked for add, remove and replace.
    _window_on(monkeypatch, date(2026, 11, 4))
    for response in (_add(s, gp, 1), s.client.delete(f"{s.base}/{gp}/pages/{two['pages'][1]['pageNo']}"),
                     _upload(s, day="2026-10-31")):
        assert response.status_code == 409 and "locked" in response.json()["detail"], response.text
    # On Tue 3 Nov it was N-3: still editable.
    _window_on(monkeypatch, date(2026, 11, 3))
    assert s.client.delete(f"{s.base}/{gp}/pages/{two['pages'][1]['pageNo']}").status_code == 200


def test_monday_photographed_apart_keeps_a_photo_per_day(setup, monkeypatch):
    s = setup
    _window_on(monkeypatch, date(2026, 11, 3))
    files = [("files", ("sun.jpg", SHARP, "image/jpeg")), ("files", ("mon.jpg", SHARP, "image/jpeg"))]
    gp = s.client.post(s.base, data={"outletId": str(s.journey.outlet_id), "businessDate": "2026-11-02",
                                     "fileDates": ["2026-11-01", "2026-11-02"]}, files=files).json()
    assert _add(s, gp["gatePassId"], 1).status_code == 422                        # which day?
    added = _add(s, gp["gatePassId"], 1, page_date="2026-11-01").json()
    assert [p["date"] for p in added["pages"]] == ["2026-11-01", "2026-11-01", "2026-11-02"]
    monday_page = next(p["pageNo"] for p in added["pages"] if p["date"] == "2026-11-02")
    refused = s.client.delete(f"{s.base}/{gp['gatePassId']}/pages/{monday_page}")
    assert refused.status_code == 409 and "Monday" in refused.json()["detail"]
    assert s.client.delete(f"{s.base}/{gp['gatePassId']}/pages/1").status_code == 200   # a Sunday one: fine


def test_a_late_register_closes_its_days_gate_pass_task_as_done_late(setup):
    """A register of an earlier day (up to N-7, 2026-11-07) finishes that day's Daily Gate Pass
    task the way an on-time one does, even after the task was marked incomplete."""
    from audit_core import task_manager as tm

    s = setup
    window = gate_pass.gate_pass_window()
    day = next(d for d in window["days"] if d < window["editFrom"])       # N-4 .. N-7
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        task_id = tm._insert_task(connection, tenant_id=s.journey.tenant_id, values={
            "scope": "OUTLET", "dealer_id": str(s.journey.dealer_id), "outlet_id": str(s.journey.outlet_id),
            "task_type": tm.GATE_PASS_TASK, "category": "DAILY_GATE_PASS", "origin": "SYSTEM",
            "source_type": "SCHEDULE", "dedupe_key": f"test-gate:{day}", "title": "Gate pass", "description": "",
            "priority": "HIGH", "assigned_role": "PC", "protocol": "SYSTEM_VERIFIED", "expected_close_minutes": 2340,
            "due": tm.gate_pass_due(day), "occurrence_date": day,
            "reference": {"businessDate": day.isoformat()},
        })
        assert tm.fail_unfinished_gate_passes(connection, tenant_id=s.journey.tenant_id) == 1
    # The worker's sweep does not reopen history; the PC finishing the register does.
    gp = _upload(s, day=day.isoformat()).json()
    assert gp["uploadedLate"] is True and gp["photosEditable"] is False
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        assert tm.close_gate_pass_tasks(connection, tenant_id=s.journey.tenant_id) == 0
    result = _register(s, gp["gatePassId"], 0, 0).json()
    assert result["nextStep"] == "DONE"
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        task = connection.execute(text("SELECT task_status, completion_result, action_completed_at_utc, due_at_utc, "
                                       "missed_at_utc FROM auditcore.p2_tasks WHERE tenant_id=:t AND task_id=:i"),
                                  {"t": s.journey.tenant_id, "i": str(task_id)}).mappings().one()
        event = connection.execute(text("SELECT details FROM auditcore.p2_task_events WHERE tenant_id=:t "
                                        "AND task_id=:i AND event_type='COMPLETED'"),
                                   {"t": s.journey.tenant_id, "i": str(task_id)}).scalar_one()
    assert task["task_status"] == "VERIFIED_COMPLETE"
    assert task["completion_result"]["outcome"] == "DONE_LATE"
    assert task["completion_result"]["gatePassId"] == gp["gatePassId"]
    assert task["action_completed_at_utc"] > task["due_at_utc"] and task["missed_at_utc"] is not None
    assert event["late"] is True
    # The counts of the late register are checked against that day's deliveries.
    assert result["deliveryFiles"] == 0 and result["divergence"] == "NONE"


def test_a_late_register_is_checked_against_its_own_days_deliveries(setup):
    s = setup
    window = gate_pass.gate_pass_window()
    day = next(d for d in window["days"] if d < window["today"] and d.isoweekday() != 1)
    _delivery_file(s, journeys=2, gate_pass_dated=day)
    _delivery_file(s, journeys=1, fresh=True)                   # today's delivery: not that day's
    gp = _upload(s, day=day.isoformat()).json()["gatePassId"]
    result = _register(s, gp, 3, 2).json()
    assert (result["deliveryFiles"], result["notIntimatedToPc"], result["taskRaised"]) == (2, 1, True)


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


def test_satellite_outlets_have_a_gate_register_too(setup):
    """Every outlet, satellites included (product owner 2026-11-02; until then onsite only)."""
    s = setup
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("UPDATE auditcore.dealer_outlets SET outlet_classification='SATELLITE' "
                                "WHERE outlet_id=:o"), {"o": s.journey.outlet_id})
    outlets = s.client.get(f"{s.base}/outlets").json()
    assert [o["outletId"] for o in outlets["outlets"]] == [str(s.journey.outlet_id)]
    assert outlets["defaultOutletId"] == str(s.journey.outlet_id) and outlets["satelliteOnly"] is False
    created = _upload(s)
    assert created.status_code == 201, created.text
    assert [o["outletId"] for o in s.client.get(f"{s.base}/summary").json()["outlets"]] == [str(s.journey.outlet_id)]


def _register(s, gate_pass_id, total, intimated):
    return s.client.post(f"{s.base}/{gate_pass_id}/counts", json={"newDeliveries": total, "intimatedToPc": intimated})


def test_total_and_intimated_settle_the_day_at_once(setup):
    """The PC enters the register's total and how many were intimated to them; the tool works out
    uploaded, pending with the PC (intimated - uploaded) and not intimated (total - intimated)."""
    s = setup
    _delivery_file(s, journeys=2)
    gp = _upload(s).json()["gatePassId"]
    assert _register(s, gp, 5, 6).status_code == 422          # more intimated than the register lists
    result = _register(s, gp, 5, 3).json()
    assert result["nextStep"] == "DONE" and result["gatePass"]["completed"] is True
    assert (result["deliveryFiles"], result["intimatedToPc"], result["pendingWithPc"],
            result["notIntimatedToPc"]) == (2, 3, 1, 2)
    assert result["gatePass"]["intimatedToPc"] == 3 and result["gatePass"]["notUploadedByPc"] == 1
    assert result["taskRaised"] is True
    # The figures every other view reads stay the same.
    totals = s.client.get(f"{s.base}/summary").json()["totals"]
    assert (totals["asPerRegister"], totals["updatedInTool"], totals["notNotifiedToPc"],
            totals["pendingDueToPc"]) == (5, 2, 2, 1)
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        task = connection.execute(text("SELECT assigned_role_code, task_status, description FROM auditcore.p2_tasks "
                                       "WHERE tenant_id=:t AND task_type='GATE_PASS_DELIVERY_NOT_NOTIFIED'"),
                                  {"t": s.journey.tenant_id}).mappings().one()
    assert task["assigned_role_code"] == "TL" and task["task_status"] == "READY"
    assert "not intimated" in task["description"]


def test_all_intimated_and_uploaded_raises_no_task(setup):
    s = setup
    _delivery_file(s, journeys=2)
    gp = _upload(s).json()["gatePassId"]
    result = _register(s, gp, 2, 2).json()
    assert result["nextStep"] == "DONE" and result["pendingWithPc"] == 0 and result["taskRaised"] is False


def test_more_uploaded_than_the_register_still_asks_which_belong_to_other_days(setup):
    s = setup
    _delivery_file(s, journeys=3)
    gp = _upload(s).json()["gatePassId"]
    result = _register(s, gp, 1, 1).json()
    assert result["nextStep"] == "EXPLAIN_EXTRA_FILES" and result["gatePass"]["completed"] is False


def test_up_to_four_pages_and_each_page_opens(setup):
    """The gate-out register can run over pages: 1 to 4 photos (product owner 2026-10-27)."""
    s = setup
    many = [("files", (f"p{n}.jpg", SHARP, "image/jpeg")) for n in range(5)]
    refused = s.client.post(s.base, data={"outletId": str(s.journey.outlet_id), "businessDate": _today()}, files=many)
    assert refused.status_code == 422 and "at most 4" in refused.json()["detail"]
    made = s.client.post(s.base, data={"outletId": str(s.journey.outlet_id), "businessDate": _today()},
                         files=many[:3])
    assert made.status_code == 201, made.text
    gp = made.json()
    assert [p["pageNo"] for p in gp["pages"]] == [1, 2, 3] and len(s.storage.objects) == 3
    third = s.client.get(f"{s.base}/{gp['gatePassId']}/image-url", params={"page": 3}).json()
    assert third["filename"].endswith("__p3.jpg") and third["pages"] == 3
    assert s.client.get(f"{s.base}/{gp['gatePassId']}/image-url", params={"page": 4}).status_code == 404
    assert s.client.get(f"{s.base}/summary").json()["outlets"][0]["days"][-1]["pages"] == 3


def test_sunday_and_monday_photographed_apart(setup, monkeypatch):
    from datetime import date

    s = setup
    monday, sunday = date(2026, 11, 2), date(2026, 11, 1)
    _window_on(monkeypatch, monday)
    files = [("files", ("sun.jpg", SHARP, "image/jpeg")), ("files", ("mon1.jpg", SHARP, "image/jpeg")),
             ("files", ("mon2.jpg", SHARP, "image/jpeg"))]
    one_day = s.client.post(s.base, data={"outletId": str(s.journey.outlet_id), "businessDate": "2026-11-02",
                                          "fileDates": ["2026-11-02", "2026-11-02", "2026-11-02"]}, files=files)
    assert one_day.status_code == 422 and "Sunday" in one_day.json()["detail"]
    made = s.client.post(s.base, data={"outletId": str(s.journey.outlet_id), "businessDate": "2026-11-02",
                                       "fileDates": ["2026-11-01", "2026-11-02", "2026-11-02"]}, files=files)
    assert made.status_code == 201, made.text
    gp = made.json()
    assert gp["photoMode"] == "SEPARATE" and [p["date"] for p in gp["pages"]] == [
        "2026-11-01", "2026-11-02", "2026-11-02"]
    day = {"stockTransfers": 0, "testDrives": 0, "otherCount": 0}
    counts = s.client.post(f"{s.base}/{gp['gatePassId']}/counts", json={"days": [
        {"date": sunday.isoformat(), "newDeliveries": 0, **day},
        {"date": monday.isoformat(), "newDeliveries": 0, **day}]}).json()
    assert counts["declared"] == 0 and counts["gatePass"]["dayCounts"][0]["date"] == "2026-11-01"


def test_a_delivery_counts_only_with_its_vehicle_gate_pass_of_that_day(setup):
    """Product owner 2026-10-27: for the Daily Gate Pass a delivery counts only when the PC
    has uploaded that vehicle's gate pass and its date matches the Daily Gate Pass date."""
    s = setup
    today = gate_pass.ist_day().date()
    _delivery_file(s, journeys=1)                                             # counts
    _delivery_file(s, journeys=1, fresh=True, gate_pass_dated=None)           # documents but no vehicle gate pass
    _delivery_file(s, journeys=1, fresh=True, gate_pass_dated=date(2026, 1, 1) if today != date(2026, 1, 1)
                   else date(2026, 1, 2))                                     # gate pass of another day
    _delivery_file(s, journeys=1, fresh=True, gate_pass_dated=today.replace(year=2022))  # year misread: not counted
    [[misread]] = _delivery_file(s, journeys=1, fresh=True, gate_pass_dated=date(2026, 3, 3) if today != date(2026, 3, 3)
                                 else date(2026, 3, 4))
    gp = _upload(s).json()["gatePassId"]
    assert _counts(s, gp, 1).json()["deliveryFiles"] == 1
    # A reviewer corrects a misread date to today: that vehicle now counts.
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        jid = connection.execute(text("SELECT journey_id FROM auditcore.p2_document_queue WHERE tenant_id=:t "
                                      "AND queue_id=:q"), {"t": s.journey.tenant_id, "q": str(misread["queue_id"])}).scalar_one()
    _vehicle_gate_pass(s, jid, date(2026, 3, 3), corrected_to=today)
    gp2 = _upload(s).json()["gatePassId"]
    assert _counts(s, gp2, 2).json()["deliveryFiles"] == 2


def test_a_removed_vehicle_gate_pass_no_longer_counts(setup):
    """Only a live vehicle gate pass counts (product owner 2026-10-27): the task, the
    calendar and the consolidated view count the same deliveries."""
    s = setup
    _delivery_file(s, journeys=2, fresh=True)
    day = gate_pass.ist_day().date()
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        assert gate_pass.delivery_file_count(connection, s.journey.tenant_id, s.journey.outlet_id, day) == 2
        connection.execute(text(
            "UPDATE auditcore.evidence SET association_status = 'VOIDED' WHERE tenant_id = :t AND evidence_id = "
            "(SELECT evidence_id FROM auditcore.evidence WHERE tenant_id = :t AND document_type_key = 'gate_pass' LIMIT 1)"),
            {"t": s.journey.tenant_id})
        assert gate_pass.delivery_file_count(connection, s.journey.tenant_id, s.journey.outlet_id, day) == 1
        assert gate_pass.vehicle_gate_pass_deliveries(
            connection, s.journey.tenant_id, [str(s.journey.outlet_id)], day, day) == {(day, str(s.journey.outlet_id)): 1}


def test_monday_counts_vehicle_gate_passes_of_sunday_too(setup):
    s = setup
    monday = date(2026, 11, 2)
    with s.journey.engine.begin() as connection:
        assert gate_pass.delivery_file_count(connection, s.journey.tenant_id, s.journey.outlet_id, monday) == 0
    _delivery_file(s, journeys=1, gate_pass_dated=date(2026, 11, 1))
    _delivery_file(s, journeys=1, fresh=True, gate_pass_dated=monday)
    _delivery_file(s, journeys=1, fresh=True, gate_pass_dated=date(2026, 10, 31))  # Saturday: its own gate pass
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        assert gate_pass.delivery_file_count(connection, s.journey.tenant_id, s.journey.outlet_id, monday) == 2


def test_consolidated_view_names_the_column_not_notified_to_pc(setup):
    s = setup
    _delivery_file(s, journeys=1)
    gp = _upload(s).json()["gatePassId"]
    _counts(s, gp, 3)
    s.client.post(f"{s.base}/{gp}/answer", json={"filesHeld": False})
    view = s.client.get(f"{s.base}/summary").json()
    assert view["totals"]["notNotifiedToPc"] == 2 == view["totals"]["pendingDueToClient"]
    assert view["outlets"][0]["days"][-1]["notNotifiedToPc"] == 2
    assert any(n.startswith("Not notified to PC") for n in view["notes"])



def test_the_pc_says_how_many_were_not_notified_and_the_rest_is_not_uploaded_by_the_pc(setup):
    """Product owner 2026-10-27: not uploaded by PC = deliveries on the gate pass
    - (found in the tool + not notified to the PC by the dealership)."""
    s = setup
    _delivery_file(s, journeys=1)
    gp = _upload(s).json()["gatePassId"]
    assert _counts(s, gp, 5).json()["difference"] == 4
    assert s.client.post(f"{s.base}/{gp}/answer", json={"notNotified": 5}).status_code == 422  # more than the gap
    answer = s.client.post(f"{s.base}/{gp}/answer", json={"notNotified": 3}).json()
    assert (answer["notNotified"], answer["notUploadedByPc"], answer["taskRaised"]) == (3, 1, True)
    assert (answer["gatePass"]["notNotifiedToPc"], answer["gatePass"]["notUploadedByPc"]) == (3, 1)
    totals = s.client.get(f"{s.base}/summary").json()["totals"]
    assert (totals["asPerRegister"], totals["updatedInTool"], totals["notNotifiedToPc"], totals["pendingDueToPc"]) == \
        (5, 1, 3, 1)
    # The calendar splits the gap the same way (product owner 2026-10-29): uploaded
    # with the day's gate pass, not intimated to the PC, and left for the PC to upload.
    from audit_core.uc03_p2_object_keys import ist_day
    calendar = s.client.get(f"{s.base}/calendar", params={"month": f"{ist_day():%Y-%m}"}).json()
    assert {k: calendar["totals"][k] for k in ("declared", "uploaded", "notNotified", "toUpload")} == \
        {"declared": 5, "uploaded": 1, "notNotified": 3, "toUpload": 1}
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        reference = connection.execute(
            text("SELECT reference FROM auditcore.p2_tasks WHERE tenant_id=:t AND category='DAILY_TASK'"),
            {"t": s.journey.tenant_id}).scalar_one()
    assert (reference["missing"], reference["notUploadedByPc"]) == (3, 1)


def test_all_notified_means_the_whole_gap_is_the_pcs_and_no_tl_task(setup):
    s = setup
    gp = _upload(s).json()["gatePassId"]
    _counts(s, gp, 2)
    answer = s.client.post(f"{s.base}/{gp}/answer", json={"notNotified": 0}).json()
    assert (answer["notUploadedByPc"], answer["taskRaised"]) == (2, False)
    assert s.client.get(f"{s.base}/summary").json()["totals"]["pendingDueToPc"] == 2
    from audit_core.uc03_p2_object_keys import ist_day
    [day] = s.client.get(f"{s.base}/calendar", params={"month": f"{ist_day():%Y-%m}"}).json()["days"]
    assert (day["notNotified"], day["toUpload"]) == (0, 2)


# ── the PC corrects the counts on the day of the upload (product owner 2026-11-03) ──
def _not_notified_task(s, gp):
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        return connection.execute(
            text("SELECT task_status, description, reference FROM auditcore.p2_tasks WHERE tenant_id=:t "
                 "AND dedupe_key=:k"), {"t": s.journey.tenant_id, "k": f"gatepass:{gp}:GATE_PASS_DELIVERY_NOT_NOTIFIED"},
        ).mappings().one_or_none()


def test_the_pc_corrects_the_counts_on_the_day_and_the_team_leads_task_follows(setup):
    s = setup
    _delivery_file(s, journeys=2)
    gp = _upload(s).json()["gatePassId"]
    first = _register(s, gp, 5, 3).json()
    assert first["gatePass"]["countsEditable"] is True and first["taskRaised"] is True
    assert _not_notified_task(s, gp)["task_status"] not in ("CANCELLED", "VERIFIED_COMPLETE")
    # Every delivery was intimated after all: the task goes.
    fixed = _register(s, gp, 5, 5)
    assert fixed.status_code == 200, fixed.text
    body = fixed.json()
    assert body["intimatedToPc"] == 5 and body["notIntimatedToPc"] == 0 and body["pendingWithPc"] == 3
    assert body["gatePass"]["countsEditCount"] == 1
    [before] = body["gatePass"]["countsHistory"]
    assert before["newDeliveries"] == 5 and before["notNotifiedToPc"] == 2
    assert _not_notified_task(s, gp)["task_status"] == "CANCELLED"
    # And back: 4 of 6 intimated -- the same task, open again, with the new numbers.
    again = _register(s, gp, 6, 4).json()
    assert again["taskRaised"] is True and again["gatePass"]["countsEditCount"] == 2
    task = _not_notified_task(s, gp)
    assert task["task_status"] == "READY" and task["reference"]["missing"] == 2 and "corrected" in task["description"]
    loaded = s.client.get(f"{s.base}/{gp}")
    assert loaded.status_code == 200 and loaded.json()["gatePass"]["newDeliveries"] == 6
    # The Daily gate pass screen lists it, so the PC can correct it right there.
    [listed] = s.client.get(f"{s.base}/outlets").json()["editableToday"]
    assert listed["gatePassId"] == gp and listed["newDeliveries"] == 6


def test_counts_are_not_corrected_after_the_day_of_the_upload(setup):
    s = setup
    gp = _upload(s).json()["gatePassId"]
    assert _register(s, gp, 2, 2).status_code == 200
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_gate_passes SET created_at_utc = now() - interval '1 day' "
                                "WHERE tenant_id=:t AND gate_pass_id=:g"), {"t": s.journey.tenant_id, "g": gp})
    late = _register(s, gp, 3, 3)
    assert late.status_code == 409 and "day the gate register was uploaded" in late.text
    assert s.client.get(f"{s.base}/outlets").json()["editableToday"] == []


def test_stock_transfers_on_the_register_are_kept_apart_from_deliveries(setup):
    """Product owner 2026-11-05: big outlets also move stock between outlets, so
    the gate register takes stock transfer entries. They are recorded and shown
    in the consolidated view, never counted as deliveries or in the gap."""
    s = setup
    _delivery_file(s, journeys=2)
    gp = _upload(s).json()["gatePassId"]
    result = s.client.post(f"{s.base}/{gp}/counts",
                           json={"newDeliveries": 2, "intimatedToPc": 2, "stockTransfers": 3})
    assert result.status_code == 200, result.text
    assert result.json()["gatePass"]["stockTransfers"] == 3
    view = s.client.get(f"{s.base}/summary").json()
    assert view["totals"]["stockTransfers"] == 3 and view["outlets"][0]["days"][-1]["stockTransfers"] == 3
    assert (view["totals"]["asPerRegister"], view["totals"]["pendingDueToPc"]) == (2, 0)
