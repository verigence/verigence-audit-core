"""The DMS booking / delivery dumps: reading each dealer's layout and matching rows
to Journeys (product owner 2026-11-13). Pure: no database, synthetic rows in the
layouts of the 5 Nov samples."""
from __future__ import annotations

import io
from datetime import date, datetime

# Excel cells carry no zone: naive datetimes are what openpyxl reads.
# ruff: noqa: DTZ001
from openpyxl import Workbook, load_workbook

from audit_core import uc03_p2_dms_dumps as m

BOOKING_HEADERS = ["Dealer Name", "Dealer Location", "Sales Consultant", "SC MIle Id", "Sales Manager",
                   "Enquiry Number", "Booking Number", "DMS Booking Number", "Booking Stage", "Created Date",
                   "Payment Status", "Booking Amount", "Customer Name", "Customer Contact Address",
                   "Customer Mobile Phone", "Customer Tehsil", "Customer District", "Customer MailingPostalCode",
                   "Product Family", "Variant Description"]


def _book(sheets: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_the_dms_booking_report_is_read_by_its_columns() -> None:
    data = _book({"FETB_EDP_v1_1": [BOOKING_HEADERS, [
        "ADITYA MOTORS", "CUTTACK", "A SC", "X", "A SM", "EN-1", "B-13455387", "OTF27A090950", "Booked",
        "02-Nov-2026 07:25 PM", datetime(2026, 11, 2), 21000, "TEST CUSTOMER", "addr", "9000000001", "t", "d",
        "753001", "XEV 9S", "THREE"], [None] * 20]})
    parsed = m.parse_workbook(data)
    assert parsed.kind == m.BOOKING
    row = parsed.rows[0]
    assert len(parsed.rows) == 1
    assert row["booking_number"] == "B-13455387" and row["dms_booking_number"] == "OTF27A090950"
    assert row["event_date"] == date(2026, 11, 2) and row["mobile"] == "9000000001"
    assert row["outlet_label"] == "CUTTACK" and row["model"] == "XEV 9S"


def test_aditya_delivery_takes_the_first_date_and_the_chassis() -> None:
    headers = ["slno", "Dealer Name", "Date", "Chassis no", "Customer Name", "Model", "SC name", "Branch",
               "Invoice no", "Date", "Varient", "Color", "Phone No", "Booking No.", "Booking Date and Time"]
    data = _book({"Sheet1": [headers, [1, "Aditya Mahindra", datetime(2026, 11, 1), "T6K14763", "TEST ONE",
                                       "MAXX CITY", "SC", "CUTTACK", "INV27A003473", datetime(2026, 10, 28),
                                       "1.5 LX", "White", 9000000002, "B-13421434", datetime(2026, 10, 28)],
                             [None] * 15]})
    parsed = m.parse_workbook(data)
    assert parsed.kind == m.DELIVERY and parsed.file_format == "DELIVERY_REPORT_INVOICED"
    row = parsed.rows[0]
    assert row["event_date"] == date(2026, 11, 1)
    assert row["chassis"] == "T6K14763" and row["mobile"] == "9000000002" and row["booking_number"] == "B-13421434"


def test_shivnath_delivery_finds_the_phone_under_a_second_name_column_and_the_outlet() -> None:
    headers = ["DATE OF DELIVERY", "Location", "CX NAME", "CX NAME", "SEGMENT", "LOCATION", "MODEL", "VARIENT",
               "COLOUR", "VIN", "REGD.NO", "CONSULTANT"]
    rows = [[datetime(2026, 11, 1), "Shivnath Mahindra", f"Person {n}", str(7000000000 + n), "PV",
             "PURIBYPASS" if n % 2 else "TAMANDO", "XUV3XO", "AX5", "White", f"T2H7633{n}", "OD02", "SC"]
            for n in range(4)]
    parsed = m.parse_workbook(_book({"Sheet1": [headers, *rows]}))
    row = parsed.rows[1]
    assert row["customer_name"] == "Person 1" and row["mobile"] == "7000000001"
    assert row["outlet_label"] == "PURIBYPASS" and row["chassis"] == "T2H76331"


def test_utkal_form_responses_are_read_from_their_own_sheet() -> None:
    headers = ["Timestamp", "Dealer Name", "Location", "\nCUSTOMER NAME ", "\nCONT NO ", "\nSALES  MANAGER ",
               "\nFSC ", "BOOKING DATE", "\nDELIVERY DATE ", "\nDocket Sign", "\nMODEL ", "\nChassis No. "]
    data = _book({"Sheet1": [[None, None], ["Row Labels", "Count"], ["CDA", 7]],
                  "Form Responses 1": [headers, [datetime(2026, 11, 1, 18, 33), "Utkal Mahindra", "CDA",
                                                 "M/s. Test ", 9379327299, "SM ", "FSC ", None,
                                                 datetime(2026, 11, 1), None, "Bolero Neo N10", "T6J57000"]]})
    parsed = m.parse_workbook(data)
    assert parsed.sheet_name == "Form Responses 1" and parsed.file_format == "DELIVERY_FORM_RESPONSES"
    row = parsed.rows[0]
    assert row["event_date"] == date(2026, 11, 1) and row["chassis"] == "T6J57000" and row["outlet_label"] == "CDA"


def test_of_two_columns_of_one_name_the_one_with_values_is_read() -> None:
    """Utkal's delivery sheet of 19/20 Nov 2026 heads two columns BOOKING DATE, the first empty:
    the empty one was read and August bookings were marked Inscope on the VIN alone."""
    headers = ["Timestamp", "Location", "CUSTOMER NAME", "CONT NO", "BOOKING DATE", "MODEL", "CHASSIS NO",
               "BOOKING DATE", "DELIVERY DATE", "FSC"]
    data = _book({"Form Responses 1": [headers, [datetime(2026, 11, 10, 14, 3), "CDA", "Ayvens India Pvt Ltd",
                                                 9437033732, None, "XUV 3XO", "T2K55617", datetime(2026, 8, 11),
                                                 datetime(2026, 11, 6), "Swati Sangita Das"]]})
    row = m.parse_workbook(data).rows[0]
    assert row["booking_date"] == date(2026, 8, 11) and row["event_date"] == date(2026, 11, 6)


def test_a_date_kept_as_iso_text_is_read() -> None:
    assert m.parse_date("2026-08-11T00:00:00") == date(2026, 8, 11)
    assert m.parse_date("2026-11-10T14:03:33.568000") == date(2026, 11, 10)
    assert m.parse_date("not a date") is None


def test_booking_keys_meet_the_booking_forms_numbers() -> None:
    keys = m.booking_keys("B-13455387", "OTF27C022672")
    assert m.journey_ref_keys("B-13455387") & keys  # Aditya prints the B-number
    assert m.journey_ref_keys("13455387") & keys  # Shivnath its digits
    assert m.journey_ref_keys("27C022672") & keys  # Utkal the OTF without OTF
    assert m.journey_ref_keys("OTF NO-22672") & keys  # or its number
    assert not m.journey_ref_keys("116")  # a short number is no key


def test_rows_match_on_number_then_chassis_then_mobile() -> None:
    index = m.JourneyIndex()
    index.journeys = {
        "j1": {"journeyId": "j1", "model": "XUV 3XO", "names": {"Test One"}, "hasDelivery": True, "outletId": None},
        "j2": {"journeyId": "j2", "model": "THAR ROXX", "names": {"Other Person"}, "hasDelivery": False,
               "outletId": None},
    }
    for k in m.journey_ref_keys("B-13455387"):
        index.by_ref[k].add("j1")
    index.by_chassis[m.chassis_key("MA1NS2NP1T2K61232")].add("j2")
    index.by_mobile["9000000003"].add("j1")
    assert m.match_row(index, m.BOOKING, {"booking_number": "B-13455387"}) == ("j1", "BOOKING_NUMBER")
    jid, basis = m.match_row(index, m.DELIVERY, {"chassis": "T2K61232"})
    assert (jid, basis) == ("j2", "CHASSIS")
    assert m.outcome_of(m.DELIVERY, {}, index.journeys[jid]) == m.NOT_IN_TOOL
    assert m.match_row(index, m.DELIVERY, {"chassis": "X", "mobile": "9000000003"}) == ("j1", "MOBILE")
    assert m.match_row(index, m.BOOKING, {"customer_name": "Nobody", "model": "XEV"}) == (None, None)
    assert m.outcome_of(m.BOOKING, {"stage": "Cancellation"}, None) == m.CANCELLED


def test_a_delivery_row_goes_by_its_chassis_before_a_misfiled_booking_form() -> None:
    # 21 Nov, Aditya: customer B's booking form was filed on customer A's Journey too, so A's
    # Journey carried B's booking number. B's delivery row took A's Journey by that number and
    # A's own row (chassis on A's gate pass) was left "Not in Verigence".
    index = m.JourneyIndex()
    index.journeys = {
        "a": {"journeyId": "a", "model": "BOLERO NEO", "names": {"Customer A"}, "hasDelivery": True, "outletId": None},
        "b": {"journeyId": "b", "model": "XUV 700", "names": {"Customer B"}, "hasDelivery": True, "outletId": None},
    }
    for k in m.journey_ref_keys("B-00000002"):  # B's form, misfiled on A
        index.by_ref[k].add("a")
    index.by_chassis[m.chassis_key("T0A00001")].add("a")
    index.by_chassis[m.chassis_key("T0A00002")].add("b")
    row_b = {"booking_number": "B-00000002", "chassis": "T0A00002", "customer_name": "Customer B"}
    row_a = {"booking_number": "B-00000001", "chassis": "T0A00001", "customer_name": "Customer A"}
    assert m.match_row(index, m.DELIVERY, row_b) == ("b", "CHASSIS")
    assert m.match_row(index, m.DELIVERY, row_a, {"b"}) == ("a", "CHASSIS")
    # No chassis on the row: the booking number still finds it.
    assert m.match_row(index, m.DELIVERY, {"booking_number": "B-00000002"}) == ("a", "BOOKING_NUMBER")


def test_dump_locations_place_rows_on_outlets() -> None:
    outlets = [{"outlet_id": "a", "outlet_name": "Aditya Motors - Khordha"},
               {"outlet_id": "b", "outlet_name": "Aditya Motors - Bhubaneswar_Cube"},
               {"outlet_id": "c", "outlet_name": "Shivnath Motors - Kesora / Puri Bypass"}]
    assert m.outlet_for("KHORDHA_SZS", outlets) == "a"
    assert m.outlet_for("BHUBANESHW", outlets) == "b"
    assert m.outlet_for("PURI_BY_PASS_SH", outlets) == "c"
    assert m.outlet_for("CSA_SEC_10_SH", outlets) is None


# --------------------------------------------------------------------------- with the database

import json
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_read_value,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="dms")
    yield created
    delete_tenant_data(engine, created.tenant_id)


def _store(journey, kind: str, rows: list[dict]) -> str:
    dump_id = str(uuid4())
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("""
            INSERT INTO auditcore.p2_dms_dumps (tenant_id, dump_id, dump_kind, dealer_id, business_date, period_month,
              file_name, file_format, row_count, uploaded_by)
            VALUES (:t, :id, :k, :d, CURRENT_DATE, date_trunc('month', CURRENT_DATE - 1)::date, 'f.xlsx', 'X', :n, 'tl')
        """), {"t": journey.tenant_id, "id": dump_id, "k": kind, "d": str(journey.dealer_id), "n": len(rows)})
        for n, row in enumerate(rows, start=1):
            connection.execute(text("""
                INSERT INTO auditcore.p2_dms_dump_rows (tenant_id, dump_id, row_no, dump_kind, dealer_id, booking_number,
                  dms_booking_number, customer_name, mobile, chassis, model, stage, event_date, raw)
                VALUES (:t, :id, :n, :k, :d, :b, :o, :c, :m, :ch, :model, :stage,
                        CURRENT_DATE - CAST(:back AS integer), CAST(:raw AS jsonb))
            """), {"t": journey.tenant_id, "id": dump_id, "n": n, "k": kind, "d": str(journey.dealer_id),
                   "b": row.get("booking_number"), "o": row.get("dms_booking_number"), "c": row.get("customer_name"),
                   "m": row.get("mobile"), "ch": row.get("chassis"), "model": row.get("model"), "stage": row.get("stage"),
                   # the day before the upload (compared), unless the row says otherwise
                   "back": row.get("days_back", 1),
                   "raw": json.dumps({})})
    return dump_id


def test_a_dump_is_matched_marked_reported_and_shown_on_the_calendar(journey) -> None:
    add_read_value(journey, field_key="booking_reference_number", value="B-13455387")
    # The chassis known from a paper that is not a delivery paper (the RC): found by the delivery
    # dump, but no delivery file on the journey (an invoice printing it would be one).
    add_read_value(journey, field_key="vin_number", value="MA1NS2NP1T2K61232", document_type="vehicle_rc")
    booking = _store(journey, m.BOOKING, [
        {"booking_number": "B-13455387", "dms_booking_number": "OTF27A090950", "customer_name": "P2 Customer"},
        {"booking_number": "B-99999999", "dms_booking_number": "OTF27A000001", "customer_name": "Someone Else"},
        {"booking_number": "B-88888888", "customer_name": "Gone", "stage": "Cancellation"},
        # dated after the upload day: outside the days compared, not counted (2026-11-18)
        {"booking_number": "B-77777777", "customer_name": "Tomorrow Only", "days_back": -1},
    ])
    delivery = _store(journey, m.DELIVERY, [{"chassis": "T2K61232", "customer_name": "P2 Customer"}])
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert m.match_dump(connection, tenant_id=journey.tenant_id, dump_id=booking) == {
            m.MATCHED: 1, m.NOT_IN_TOOL: 0, m.MISSING: 1, m.CANCELLED: 1}
        # Found, but no delivery file on the journey yet.
        assert m.match_dump(connection, tenant_id=journey.tenant_id, dump_id=delivery)[m.NOT_IN_TOOL] == 1
        # Checking again changes nothing and keeps the first sync time.
        first = connection.execute(text("SELECT dms_booking_synced_at FROM auditcore.p2_journey_dms_sync "
                                        "WHERE journey_id = :j"), {"j": journey.journey_id}).scalar_one()
        m.match_dump(connection, tenant_id=journey.tenant_id, dump_id=booking)
        sync = connection.execute(text("SELECT * FROM auditcore.p2_journey_dms_sync WHERE journey_id = :j"),
                                  {"j": journey.journey_id}).mappings().one()
        assert sync["dms_booking_synced_at"] == first and sync["dms_booking_dump_id"] is not None
        assert sync["dms_delivery_synced_at"] is not None
        today_db = connection.execute(text("SELECT CURRENT_DATE")).scalar_one()
        data = m.build_report(connection, tenant_id=journey.tenant_id, dealer_ids=[str(journey.dealer_id)],
                              first=today_db.replace(day=1))
        book = load_workbook(io.BytesIO(data))
        assert book.sheetnames[:3] == ["Summary", "Booking dump variance", "Delivery dump variance"]
        missing = [r for r in book["Booking dump variance"].iter_rows(values_only=True)][1:]
        assert [r[8] for r in missing] == ["Someone Else"]  # the cancelled booking is not missing
        assert [r[4] for r in book["Delivery dump variance"].iter_rows(values_only=True)][1:] == [
            "Booking on file, delivery not uploaded"]
        # One day's uploads only (2026-11-18): a day with no upload gives an empty report.
        today_dumps = m.build_report(connection, tenant_id=journey.tenant_id, dealer_ids=[str(journey.dealer_id)],
                                     first=today_db.replace(day=1), day=today_db)
        assert load_workbook(io.BytesIO(today_dumps))["Summary"].max_row > 1
        other_day = today_db.replace(day=1) if today_db.day != 1 else today_db.replace(day=2)
        none = m.build_report(connection, tenant_id=journey.tenant_id, dealer_ids=[str(journey.dealer_id)],
                              first=today_db.replace(day=1), day=other_day)
        assert load_workbook(io.BytesIO(none))["Summary"].max_row == 1
        calendar = m.dump_calendar(connection, tenant_id=journey.tenant_id, dealer_ids=[str(journey.dealer_id)],
                                   first=today_db.replace(day=1), today=today_db)
        today = next(d for d in calendar["dealers"][0]["days"] if d["date"] == today_db.isoformat())
        assert today["booking"]["status"] == "UPLOADED" and today["booking"]["missing"] == 1


def test_a_team_lead_s_bundle_is_enough_to_upload() -> None:
    """A Team Lead's bundle has audit.work.update and no audit.journey.update: asking for
    journey.update alone refused every TL upload (2026-11-14, VAC-AUTH-002)."""
    assert "audit.work.update" in m._UPDATE and "audit.journey.update" in m._UPDATE
    assert set(m.UPLOAD_ROLES) == {"TL", "REVIEWER"}


def test_a_model_spaced_or_extended_differently_is_the_same_car() -> None:
    # 2026-11-18: Shivnath's bookings on file were reported "not in Verigence" because the
    # model names were spaced differently in the DMS and on the papers.
    assert m.same_model("XEV9S", "XEV 9S")
    assert m.same_model("BE6 sportber threet 79 NCH", "BE 6")
    assert m.same_model("3XO", "XUV3XO EV")
    assert not m.same_model("THAR ROXX", "XUV 3XO")
    assert not m.same_model("BE", "BE 6")  # too short to stand for a model
    index = m.JourneyIndex()
    index.journeys = {"j1": {"journeyId": "j1", "model": "XEV9S", "names": {"RENUBALA ROUT"}, "hasDelivery": False,
                             "outletId": None}}
    assert m.match_row(index, m.BOOKING, {"customer_name": "RENUBALA ROUT", "model": "XEV 9S"}) == ("j1", "NAME_MODEL")


def test_a_booking_row_matches_on_the_name_and_the_booking_date() -> None:
    # 2026-11-18: no OTF and no mobile on the papers; the DMS booking date tells two
    # customers of one name apart, whatever the model is called.
    from datetime import date

    index = m.JourneyIndex()
    index.journeys = {
        "j1": {"journeyId": "j1", "model": "XEV9S", "names": {"RENUBALA ROUT"}, "hasDelivery": False,
               "outletId": None, "bookingDays": {date(2026, 11, 7)}},
        "j2": {"journeyId": "j2", "model": "THAR", "names": {"Renubala Rout"}, "hasDelivery": False,
               "outletId": None, "bookingDays": {date(2026, 9, 1)}},
    }
    row = {"customer_name": "RENUBALA ROUT", "model": "Something else", "event_date": date(2026, 11, 8)}
    assert m.match_row(index, m.BOOKING, row) == ("j1", "NAME_DATE")
    # a month away from both, a different model: no Journey is taken on the name alone
    assert m.match_row(index, m.BOOKING, {**row, "event_date": date(2026, 10, 10)}) == (None, None)


def test_the_name_and_a_mobile_or_booking_number_a_digit_or_two_off_find_the_customer() -> None:
    """2026-11-21, Aditya's Sujit Kumar Bastia: the booking form read 9337354361 for 9337359361,
    B-1348698 for B-13486428, its date as 2020 and the model XUV-9S for XEV 9S."""
    from datetime import date

    def journey(jid: str, name: str, mobiles: set[str], refs: set[str]) -> dict:
        return {"journeyId": jid, "model": "XUV-9S", "names": {name}, "hasDelivery": False, "outletId": None,
                "bookingDate": None, "bookingDays": {date(2026, 11, 20)}, "mobiles": mobiles, "refs": refs}

    index = m.JourneyIndex()
    index.journeys = {"j1": journey("j1", "Sujit Kumar Bastia", {"9337354361"}, {"1348698"}),
                      "j2": journey("j2", "Anita Sahoo", {"9337359361"}, set())}
    row = {"customer_name": "Sujit Kumar bastia", "mobile": "9337359361", "booking_number": "B-13486428",
           "dms_booking_number": "OTF27A092698", "model": "XEV 9S", "event_date": date(2026, 11, 7)}
    # j2 holds the DMS mobile but is another customer: the mobile alone takes it -- so the
    # misread case is tested with the exact mobile on no Journey.
    del index.journeys["j2"]
    assert m.match_row(index, m.BOOKING, row) == ("j1", "NAME_NEAR_ID")
    # the booking number alone a digit or two off
    assert m.match_row(index, m.BOOKING, {**row, "mobile": "7000000000"}) == ("j1", "NAME_NEAR_ID")
    # the same name, a mobile and booking number far off: another customer, not taken
    assert m.match_row(index, m.BOOKING, {**row, "mobile": "7000000000", "booking_number": "B-13999999",
                                          "dms_booking_number": None}) == (None, None)
    # two Journeys of that name, both near: not decided on the name
    index.journeys["j3"] = journey("j3", "SUJIT KUMAR BASTIA", {"9337359362"}, set())
    assert m.match_row(index, m.BOOKING, row) == (None, None)
    assert m.near_mobile("9337359361", {"9337354361"}) and not m.near_mobile("9337359361", {"9437000361"})
    assert m.near_booking({"13486428"}, {"1348698"}) and not m.near_booking({"21913"}, {"21914"})


def test_name_and_date_use_the_booking_form_or_the_first_receipt_within_two_days() -> None:
    """The booking date (product owner 2026-11-22): the booking form's date or the first money
    receipt's, within two days of the DMS's -- never the upload day (the index carries none)."""
    from datetime import date

    index = m.JourneyIndex()
    index.journeys = {"j1": {"journeyId": "j1", "model": "THAR", "names": {"Pankaj Dutta"}, "hasDelivery": False,
                             "outletId": None, "bookingDate": None,
                             # no booking form date read; the first money receipt of the 10th
                             "bookingDays": {date(2026, 11, 10)}, "mobiles": set(), "refs": set()}}
    row = {"customer_name": "PANKAJ DUTTA", "model": "XUV 7XO", "event_date": date(2026, 11, 8)}
    assert m.match_row(index, m.BOOKING, row) == ("j1", "NAME_DATE")              # two days off
    assert m.match_row(index, m.BOOKING, {**row, "event_date": date(2026, 11, 7)}) == (None, None)  # three
    # the booking form's date or the receipt's: either within two days
    index.journeys["j1"]["bookingDays"] = {date(2026, 11, 1), date(2026, 11, 10)}
    assert m.match_row(index, m.BOOKING, {**row, "event_date": date(2026, 11, 2)}) == ("j1", "NAME_DATE")
    # nothing read: no date to match on
    index.journeys["j1"]["bookingDays"] = set()
    assert m.match_row(index, m.BOOKING, row) == (None, None)


def test_a_dump_is_compared_from_the_first_to_its_upload_day() -> None:
    """2026-11-18: uploaded on the 18th, compared for the 1st to the 18th."""
    from datetime import date

    assert m.dump_window(date(2026, 11, 1), date(2026, 11, 18)) == (date(2026, 11, 1), date(2026, 11, 18))
    # uploaded on 1 Dec with November's rows: the whole of November
    assert m.dump_window(date(2026, 11, 1), date(2026, 12, 1)) == (date(2026, 11, 1), date(2026, 11, 30))
