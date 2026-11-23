"""The DMS dumps correct what the Journey's papers were read as (product owner 2026-11-23):
the sales consultant, the OTF, the enquiry number and the PIN from the booking dump; the DMS
invoice number and the sales consultant from the delivery dump. Only on a trusted match --
booking: the OTF and the name, and the mobile or the model; delivery: the OTF, the model and
the name. All values here are made up."""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text
from test_dms_intimations import _today, setup  # noqa: F401 -- the fixture
from test_dms_strong_match_sync import _document, _log, _value

from audit_core import uc03_p2_dms_dumps as dumps
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_dms_field_sync import otf_target, same_vehicle


def _dump(s, kind: str, rows: list[dict]) -> str:
    dump_id = str(uuid4())
    today = _today()
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("""
            INSERT INTO auditcore.p2_dms_dumps (tenant_id, dump_id, dump_kind, dealer_id, business_date, period_month,
              file_name, file_format, row_count, uploaded_by)
            VALUES (:t, :id, :k, :d, CAST(:today AS date), date_trunc('month', CAST(:today AS date) - 2)::date,
                    'f.xlsx', 'X', :n, 'rv')
        """), {"t": s.journey.tenant_id, "id": dump_id, "k": kind, "d": str(s.journey.dealer_id), "n": len(rows),
               "today": today})
        for n, row in enumerate(rows, start=1):
            connection.execute(text("""
                INSERT INTO auditcore.p2_dms_dump_rows (tenant_id, dump_id, row_no, dump_kind, dealer_id, booking_number,
                  dms_booking_number, customer_name, mobile, chassis, event_date, model, consultant, invoice_number, raw)
                VALUES (:t, :id, :n, :k, :d, :b, :o, :c, :m, :ch, CAST(:today AS date), :model, :sc, :inv,
                        CAST(:raw AS jsonb))
            """), {"t": s.journey.tenant_id, "id": dump_id, "n": n, "k": kind, "d": str(s.journey.dealer_id),
                   "b": row.get("booking_number"), "o": row.get("otf"), "c": row.get("name"), "m": row.get("mobile"),
                   "ch": row.get("chassis"), "today": today, "model": row.get("model"), "sc": row.get("consultant"),
                   "inv": row.get("invoice"), "raw": json.dumps(row.get("raw") or {})})
        dumps.match_dump(connection, tenant_id=s.journey.tenant_id, dump_id=dump_id)
    return dump_id


def _fields(s, dump_id: str) -> list[dict]:
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        ds = connection.execute(text("SELECT date_sync FROM auditcore.p2_dms_dump_rows WHERE tenant_id = :t "
                                     "AND dump_id = :d AND row_no = 1"),
                                {"t": s.journey.tenant_id, "d": dump_id}).scalar_one()
    return (ds or {}).get("fields", [])


def _synced(s) -> list[str]:
    return sorted(e["field_key"] for e in _log(s) if e["change_reason"] == "DMS_SYNC")


def _booking_papers(s) -> tuple[str, str]:
    docket = _document(s, "booking_form", booking_reference_number="019876", sales_person="N. P. Someone",
                       enquiry_number="EN-0038000001", customer_phone="9000000101")
    aadhaar = _document(s, "aadhaar", stage="BOOKING", address_pincode="759120")
    return docket, aadhaar


_BOOKING_ROW = {"booking_number": "B-77700021", "otf": "OTF27A019876", "name": "P2 CUSTOMER",
                "mobile": "9000000101", "model": "THAR ROXX", "consultant": "TEST KUMAR TESTER",
                "raw": {"ENQUIRY NUMBER": "EN-0038000002", "CUSTOMER MAILINGPOSTALCODE": "759124"}}


def test_a_trusted_booking_match_corrects_the_consultant_otf_enquiry_and_pin(setup) -> None:  # noqa: F811
    s = setup
    docket, aadhaar = _booking_papers(s)
    dump_id = _dump(s, dumps.BOOKING, [_BOOKING_ROW])
    assert _value(s, docket, "booking_reference_number") == "OTF27A019876"  # the full OTF, same kind
    assert _value(s, docket, "sales_person") == "TEST KUMAR TESTER"
    assert _value(s, docket, "enquiry_number") == "EN-0038000002"
    assert _value(s, aadhaar, "address_pincode") == "759124"
    assert _synced(s) == ["address_pincode", "booking_reference_number", "enquiry_number", "sales_person"]
    assert {f["field"]: f["status"] for f in _fields(s, dump_id)} == {
        "address_pincode": "APPLIED", "booking_reference_number": "APPLIED", "enquiry_number": "APPLIED",
        "sales_person": "APPLIED"}
    # Matched again: nothing more to correct.
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        dumps.match_dump(connection, tenant_id=s.journey.tenant_id, dump_id=dump_id)
    assert len(_synced(s)) == 4


def test_a_different_name_corrects_nothing(setup) -> None:  # noqa: F811
    s = setup
    docket, _ = _booking_papers(s)
    _dump(s, dumps.BOOKING, [{**_BOOKING_ROW, "name": "Someone Else Entirely"}])
    assert _value(s, docket, "sales_person") == "N. P. Someone"
    assert _synced(s) == []


def test_neither_the_mobile_nor_the_model_agreeing_corrects_nothing(setup) -> None:  # noqa: F811
    s = setup
    docket, _ = _booking_papers(s)
    _dump(s, dumps.BOOKING, [{**_BOOKING_ROW, "mobile": "9111111111"}])  # the Journey has no model
    assert _value(s, docket, "sales_person") == "N. P. Someone"
    assert _synced(s) == []


def test_the_same_consultant_spelt_otherwise_is_left(setup) -> None:  # noqa: F811
    s = setup
    docket = _document(s, "booking_form", booking_reference_number="B-77700022", sales_person="Ravi Ku. Testpal",
                       customer_phone="9000000102")
    _dump(s, dumps.BOOKING, [{**_BOOKING_ROW, "booking_number": "B-77700022", "otf": "OTF27A000022",
                              "mobile": "9000000102", "consultant": "RAVI KUMAR TESTPAL", "raw": {}}])
    assert _value(s, docket, "sales_person") == "Ravi Ku. Testpal"
    assert _synced(s) == []


def test_a_trusted_delivery_match_corrects_the_dms_invoice_number_and_the_vin(setup) -> None:  # noqa: F811
    s = setup
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("INSERT INTO auditcore.journey_products (tenant_id, journey_id, model_name_snapshot) "
                                "VALUES (:t, :j, 'Thar Roxx') ON CONFLICT (tenant_id, journey_id) "
                                "DO UPDATE SET model_name_snapshot = EXCLUDED.model_name_snapshot"),
                           {"t": s.journey.tenant_id, "j": s.journey.journey_id})
    docket = _document(s, "booking_form", booking_reference_number="B-77700031", sales_person="Someone Wrong")
    invoice = _document(s, "customer_invoice_dms", stage="DELIVERY", invoice_number="INV-2600l234",
                        vin_number="MA1XXAAAAT2K5563B")
    gate = _document(s, "gate_pass", stage="DELIVERY", vin_number="MA1XXAAAAT2K5563B")
    # The sheet prints the chassis's last 8 characters, the paper read one of them wrong.
    row = {"booking_number": "B-77700031", "name": "P2 Customer", "model": "THAR ROXX", "chassis": "T2K55631",
           "consultant": "TEST KUMAR TESTER", "invoice": "INV-26001234"}
    _dump(s, dumps.DELIVERY, [row])
    assert _value(s, invoice, "invoice_number") == "INV-26001234"
    assert _value(s, invoice, "vin_number") == "MA1XXAAAAT2K55631"
    assert _value(s, gate, "vin_number") == _value(s, invoice, "vin_number")
    assert _value(s, docket, "sales_person") == "Someone Wrong"  # the delivery dump's consultant is not taken
    assert _synced(s) == ["invoice_number", "vin_number", "vin_number"]


def test_a_delivery_row_without_the_otf_corrects_nothing(setup) -> None:  # noqa: F811
    s = setup
    invoice = _document(s, "customer_invoice_dms", stage="DELIVERY", invoice_number="INV-2600l234",
                        vin_number="MA1XX2T2K55632")
    _dump(s, dumps.DELIVERY, [{"name": "P2 Customer", "model": "THAR ROXX", "chassis": "T2K55632",
                               "invoice": "INV-26001234"}])
    assert _value(s, invoice, "invoice_number") == "INV-2600l234"
    assert [e for e in _synced(s) if e != "delivery_date"] == []


def test_the_otf_written_is_the_dms_number_of_the_kind_read() -> None:
    row = {"booking_number": "B-77700041", "dms_booking_number": "OTF27A031415"}
    assert otf_target("77700041", row) == "B-77700041"
    assert otf_target("OTF 31415", row) == "OTF27A031415"
    assert otf_target("031415", row) == "OTF27A031415"
    assert otf_target("55555", row) is None


def test_same_vehicle_is_stricter_than_a_word_in_common() -> None:
    assert same_vehicle("SCORPIO-N", "SCORPION Z8L D")
    assert same_vehicle("XUV3XO", "3XO")
    assert not same_vehicle("XUV 7XO", "XUV 3XO")
    assert not same_vehicle("XEV 9S", "XUV 3XO")


def test_journey_360_shows_the_dms_enquiry_number_when_the_papers_have_none(setup) -> None:  # noqa: F811
    from audit_core.uc03_p2_journey360 import vehicle

    s = setup
    _document(s, "booking_form", booking_reference_number="B-77700051", customer_phone="9000000105")
    _dump(s, dumps.BOOKING, [{**_BOOKING_ROW, "booking_number": "B-77700051", "otf": "OTF27A000051",
                              "mobile": "9000000105", "raw": {"ENQUIRY NUMBER": "EN-0038000051"}}])
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        booking = vehicle(connection, tenant_id=s.journey.tenant_id, journey_id=s.journey.journey_id)["booking"]
    assert (booking["enquiryNumber"], booking["enquiryNumberFromDms"]) == ("EN-0038000051", True)


def test_the_vin_written_is_the_dms_vin_or_the_paper_s_with_the_dms_last_characters() -> None:
    from audit_core.uc03_p2_dms_dumps import CHASSIS_DOCUMENT_TYPES
    from audit_core.uc03_p2_dms_field_sync import CHASSIS_DOCS, vin_target

    assert CHASSIS_DOCS == CHASSIS_DOCUMENT_TYPES
    assert vin_target("MA1AAAAAAAAB12345X", "B1234567") is None  # 18 read: not a VIN
    assert vin_target("MA1AAAAAAAB12345X", "B1234567") == "MA1AAAAAAB1234567"
    assert vin_target("MA1AAAAAAAB12345X", "MA1ZZZZZZZB123456") == "MA1ZZZZZZZB123456"
    assert vin_target("B12345X", "B1234567") is None
