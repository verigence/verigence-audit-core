"""The PCs mark which DMS dump rows were intimated to them; the report is generated on
demand by the Reviewer and above from those marks (product owner 2026-11-18). Synthetic
rows only."""
from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from p2_support import (
    AllowAllAuthorization,
    add_extracted_field,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_p2_dms_dumps as dumps
from audit_core import uc03_p2_dms_intimations as m
from audit_core import uc03_p2_dms_outlet_report as report
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client


def test_a_row_is_known_by_its_otf_or_chassis_across_dumps() -> None:
    assert m.row_key(m.BOOKING, {"dms_booking_number": "otf-27a 0909", "booking_number": "B-1"}) == "OTF:OTF27A0909"
    assert m.row_key(m.BOOKING, {"booking_number": "B-13455387"}) == "BKG:B13455387"
    assert m.row_key(m.DELIVERY, {"chassis": "t2k61232", "dms_booking_number": "OTF1"}) == "CH:T2K61232"
    assert m.row_key(m.DELIVERY, {"invoice_number": "INV27A003473"}) == "INV:INV27A003473"
    nameless = m.row_key(m.BOOKING, {"customer_name": "Test One", "mobile": "+91 90000 00001",
                                     "event_date": date(2026, 11, 2)})
    assert nameless == "NM:TESTONE|9000000001|2026-11-02"


def test_the_statement_names_outlets_days_and_absent_pcs() -> None:
    dealers = [{"dealerName": "Utkal", "statement": {
        "registerMissing": {
            "ONSITE": [{"outlet": "KEONJHAR", "days": 3, "dates": ["2026-11-04", "2026-11-09", "2026-11-12"],
                        "pcAbsentDays": 1}],
            "SATELLITE": []},
        "pcNotAvailable": [{"outlet": "ANANDAPUR", "noPcMapped": True, "days": 0, "dates": [], "leaveDays": 0},
                           {"outlet": "KEONJHAR", "noPcMapped": False, "days": 1, "dates": ["2026-11-09"],
                            "leaveDays": 1}]}}]
    assert report.statement_lines(dealers) == [
        "Utkal",
        "* Onsite - gate register missing: KEONJHAR 3 days (4, 9, 12 Nov), PC absent on 1",
        "* Satellite - gate register missing: none",
        "* PC not available (outlets impacted): ANANDAPUR (no PC mapped); KEONJHAR 1 day (9 Nov), 1 on leave",
    ]


@pytest.fixture
def setup():
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="intim")
    reviewer = f"intim-rv-{uuid4().hex[:8]}"
    with engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, "
                                "business_role_code, dealer_id) VALUES (:t, :a, 'REVIEWER', :d)"),
                           {"t": journey.tenant_id, "a": reviewer, "d": journey.dealer_id})
    acting = {"subject": journey.actor_id}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=acting["subject"])
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        yield SimpleNamespace(journey=journey, reviewer=reviewer, acting=acting,
                              client=TestClient(app, raise_server_exceptions=False),
                              base=f"/p2/v1/tenants/{journey.tenant_id}")
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, journey.tenant_id)


def _today() -> date:
    """Today as the Project counts it (its time zone, Asia/Kolkata here), the day the DMS code
    works by (uc03_p2_dms_dumps._today). Not the database's CURRENT_DATE: that is the UTC day,
    one day behind from 00:00 to 05:30 IST, when a report "for today" then found no dump."""
    return datetime.now(UTC).astimezone(ZoneInfo("Asia/Kolkata")).date()


def _dump(s, kind: str, rows: list[dict], back: int = 0) -> str:
    """A dump uploaded ``back`` days ago, its rows dated the day before."""
    dump_id = str(uuid4())
    today = _today()
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("""
            INSERT INTO auditcore.p2_dms_dumps (tenant_id, dump_id, dump_kind, dealer_id, business_date, period_month,
              file_name, file_format, row_count, uploaded_by)
            VALUES (:t, :id, :k, :d, CAST(:today AS date) - CAST(:b AS integer),
                    date_trunc('month', CAST(:today AS date) - 2)::date, 'f.xlsx', 'X', :n, 'rv')
        """), {"t": s.journey.tenant_id, "id": dump_id, "k": kind, "d": str(s.journey.dealer_id), "n": len(rows),
               "b": back, "today": today})
        for n, row in enumerate(rows, start=1):
            connection.execute(text("""
                INSERT INTO auditcore.p2_dms_dump_rows (tenant_id, dump_id, row_no, dump_kind, dealer_id, booking_number,
                  dms_booking_number, customer_name, mobile, chassis, event_date, raw)
                VALUES (:t, :id, :n, :k, :d, :b, :o, :c, :m, :ch, CAST(:today AS date) - CAST(:back AS integer),
                        '{}')
            """), {"t": s.journey.tenant_id, "id": dump_id, "n": n, "k": kind, "d": str(s.journey.dealer_id),
                   "b": row.get("booking_number"), "o": row.get("otf"), "c": row.get("name"), "m": row.get("mobile"),
                   "ch": row.get("chassis"), "back": row.get("back", back), "today": today})
        dumps.match_dump(connection, tenant_id=s.journey.tenant_id, dump_id=dump_id)
    return dump_id


@pytest.mark.skipif(_today().day < 3, reason="needs two dump days in the month")
def test_pcs_mark_rows_the_marks_carry_and_the_reviewer_generates_the_report(setup) -> None:
    s = setup
    add_extracted_field(s.journey, di_document_id=uuid4(), field_key="booking_reference_number", value="B-13455387")
    # Yesterday's dump: our booking and one not in Verigence.
    _dump(s, m.BOOKING, [{"booking_number": "B-13455387", "otf": "OTF27A090950", "name": "P2 Customer", "back": 2},
                         {"otf": "OTF27A000001", "name": "Asha Das", "mobile": "9000000001", "back": 2}], back=1)
    # Today's dump: the same two, and a new one dated today.
    today_id = _dump(s, m.BOOKING, [
        {"booking_number": "B-13455387", "otf": "OTF27A090950", "name": "P2 Customer", "back": 2},
        {"otf": "OTF27A000001", "name": "Asha Das", "mobile": "9000000001", "back": 2},
        {"otf": "OTF27A000002", "name": "Bina Rout", "mobile": "9000000002", "back": 0}])

    # The PC sees the rows since the last dump; the earlier open row apart; search over the month.
    listed = s.client.get(f"{s.base}/dms-intimations", params={"kind": "BOOKING"}).json()
    assert [i["customerName"] for i in listed["items"]] == ["Bina Rout"]
    assert listed["counts"] == {"new": 1, "open": 1, "month": 3}
    found = s.client.get(f"{s.base}/dms-intimations", params={"kind": "BOOKING", "q": "90000 00001"}).json()
    assert [i["customerName"] for i in found["items"]] == ["Asha Das"]
    in_tool = s.client.get(f"{s.base}/dms-intimations", params={"kind": "BOOKING", "q": "OTF27A090950"}).json()
    assert in_tool["items"][0]["state"] == m.IN_VERIGENCE

    # A row in Verigence needs no mark; the others are marked, once.
    rows = {i["customerName"]: i for i in s.client.get(
        f"{s.base}/dms-intimations", params={"kind": "BOOKING", "view": "month"}).json()["items"]}
    assert s.client.post(f"{s.base}/dms-intimations/claim", json={
        "dumpId": today_id, "rowNo": rows["P2 Customer"]["rowNo"]}).status_code == 409
    asha = {"dumpId": today_id, "rowNo": rows["Asha Das"]["rowNo"]}
    assert s.client.post(f"{s.base}/dms-intimations/claim", json=asha).json()["state"] == m.INTIMATED
    assert s.client.post(f"{s.base}/dms-intimations/withdraw", json=asha).json()["state"] == m.OPEN
    assert s.client.post(f"{s.base}/dms-intimations/claim", json=asha).status_code == 200

    # The mark is kept by the OTF: the next day's dump finds it marked.
    tomorrow = _dump(s, m.BOOKING, [{"otf": "OTF27A000001", "name": "ASHA DAS", "back": 2}])
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        [row] = m.dump_rows(connection, s.journey.tenant_id, tomorrow)
        marks = m.live_marks(connection, s.journey.tenant_id, [str(s.journey.dealer_id)])
        assert m.row_state(row, marks.get((str(s.journey.dealer_id), m.BOOKING, m.row_key(m.BOOKING, row)))) == \
            m.INTIMATED
        connection.execute(text("UPDATE auditcore.p2_dms_dumps SET status = 'SUPERSEDED' WHERE dump_id = :d"),
                           {"d": tomorrow})

    # Only the Reviewer and above see the counter and generate the report.
    assert s.client.get(f"{s.base}/dms-intimations/counter", params={"kind": "BOOKING"}).status_code == 403
    assert s.client.post(f"{s.base}/dms-reports", json={}).status_code == 403
    s.acting["subject"] = s.reviewer
    [dealer] = s.client.get(f"{s.base}/dms-intimations/counter", params={"kind": "BOOKING"}).json()["dealers"]
    assert dealer["totals"] == {"rows": 3, "inVerigence": 1, "intimated": 1, "open": 1, "newRows": 1, "newOpen": 1}
    [pc] = dealer["pcs"]
    assert pc["actorId"] == s.journey.actor_id and pc["marked"] == 1 and pc["markedNew"] == 0

    made = s.client.post(f"{s.base}/dms-reports", json={})
    assert made.status_code == 201, made.text
    figures = made.json()["figures"]
    assert (figures["bookingDms"], figures["bookingSynced"], figures["bookingIntimated"],
            figures["bookingNotIntimated"]) == (3, 1, 1, 1)
    listed_reports = s.client.get(f"{s.base}/dms-reports").json()["items"]
    assert [r["reportId"] for r in listed_reports] == [made.json()["reportId"]]
    kept = s.client.get(f"{s.base}/dms-reports/{made.json()['reportId']}").json()
    assert {r["customerName"]: r["intimation"] for r in kept["rows"]} == {
        "Asha Das": "Intimated to PC - pending with PC", "Bina Rout": "Not intimated to PC - pending with dealership"}
    book = load_workbook(io.BytesIO(s.client.get(f"{s.base}/dms-reports/{made.json()['reportId']}/file").content))
    assert book.sheetnames[:2] == ["Outlet report", "Statement"]
    assert "Bookings not in Verigence" in book.sheetnames and "PC intimations" in book.sheetnames
    # A day with no dump: nothing to generate.
    assert s.client.post(f"{s.base}/dms-reports",
                         json={"date": _today().replace(day=1).isoformat()}).status_code == 409


def test_an_upload_alerts_the_dealership_s_pcs(setup) -> None:
    s = setup
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        for _ in range(2):  # a second upload replaces the first alert
            m.publish_dump_alert(connection, tenant_id=s.journey.tenant_id, dealer_id=str(s.journey.dealer_id),
                                 dealer_name="D", kind=m.BOOKING, business_date=date(2026, 11, 18))
        live = connection.execute(text(
            "SELECT title, body, payload FROM auditcore.p2_tenant_alerts WHERE tenant_id = :t "
            "AND alert_kind = 'DMS_DUMP_UPLOADED' AND expires_at_utc > now()"), {"t": s.journey.tenant_id}).all()
    [(title, body, payload)] = live
    assert title == "Booking dump has been uploaded" and "Pls update your booking intimations" in body
    payload = payload if isinstance(payload, dict) else json.loads(payload)
    assert payload["audienceRoles"] == ["PC"] and payload["audienceOutletIds"] == [str(s.journey.outlet_id)]
    alerts = s.client.get(f"{s.base}/alerts")
    if alerts.status_code == 200:  # the PC of the outlet sees it
        assert any(a["kind"] == "DMS_DUMP_UPLOADED" for a in alerts.json()["items"])
