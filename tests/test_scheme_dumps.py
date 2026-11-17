"""The monthly extended warranty (EW) and RSA dumps (product owner 2026-11-17).
Pure: no database. Synthetic rows in the layout of Mahindra's scheme registers."""
# Excel hands a date cell over as a naive datetime: the synthetic rows do the same.
# ruff: noqa: DTZ001
from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from openpyxl import Workbook, load_workbook

from audit_core import uc03_p2_scheme_dumps as m
from audit_core.uc03_p2_dms_dumps import (
    JourneyIndex,
    customer_code_key,
    registration_key,
)

EW_HEAD = ["GSTIN/UIN of Customer", "Customer ID", "SCHEME REGISTRATION ID", "Mahindra GST no", "SCHEME TYPE",
           "EMPLOYEE NAME", "EMPLOYEE DESIGNATION", "Invoice Number", "Scheme Reg. Date", "Model Family",
           "Scheme Desc.", "Sold By", "Category", "Vehicle Reg. Number", "Chasiss Number", "Km", "Accessible Amount",
           "Discount Amount", "Final Scheme Amount", "GST Tax Rate", "SGST Amount", "CGST Amount", "IGST Amount",
           "Incentive accessible Amount", "GST Tax Rate", "SGST Amount", "CGST Amount", "IGST Amount",
           "Incentive Amount", "Scheme Status", "Model Description", "Cancelled Amount", "Cancelled Date", "Location"]
RSA_HEAD = ["GSTIN/UIN of Customer", "Customer ID", "Mahindra GSTN", "Scheme Invoice No", "Scheme Reg Date",
            "Model Family", "Scheme desc", "Vehicle Reg No", "Chassis No", "KM", "Employee Name",
            "Employee Designation", "Sold By", "Status", "Model Desc", "RSA Cancelled Date", "RSA Cancelled Amount",
            "Incentive Accessible amount", "GST Tax Rate", "SGST Amount", "CGST Amount", "IGST Amount",
            "Incentive Total Amount", "Accessible Amount", "RSA Discount", "Amount", "GST Tax Rate", "SGST Amount",
            "CGST Amount", "IGST Amount", "Total RSA amount", "HSN/SAC", "Location"]


def _xlsx(head: list[str], rows: list[list], *, lead: list[list] | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    for line in lead or []:
        ws.append(line)
    ws.append(head)
    for r in rows:
        ws.append(r)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _ew(number: str, chassis: str, *, sold_by: str = "SALS", category: str = "PAID-WITHIN WARRANTY",
        reg: str = "OD02DV8990", when=datetime(2026, 10, 14), status: str = "Completed", cancelled=None,
        location: str | None = "Tamando", designation: str = "Field Sales Consultant - Personal") -> list:
    return [None, "R270905009", number, "27AAACM3025E1ZZ", "Scheme Warranty Registration", "A PERSON", designation,
            1648133175, when, "J71 - XUV 7XO Diesel MT", "SHIELD -4TH YR EXTENDED WARRANTY - XUV7XO", sold_by, category,
            reg, chassis, 11, 19745, 0, 19745, 18, None, None, 3554.1, 1271.19, 0.18, 0, 0, 228.81, 1500, status, None,
            None, cancelled, location]


def _rsa(number: str, chassis: str, *, sold_by: str | None = "SALS", designation: str = "Field Sales Consultant",
         reg: str = "OD05CN9040", when=datetime(2026, 10, 14), code: str = "R270905009", location: str | None = None,
         cancelled=None) -> list:
    return [None, code, "27AAACM3025E1ZZ", number, when, "J46 - XUV 3XO GSL TCMPFI MT", "\xa0RSA Scheme for 4th Year",
            reg, chassis, 11, "A PERSON", designation, sold_by, "Completed", "XUV3XO AX5 PM MT RP", cancelled, None,
            287.29, 18, 0, 0, 51.71, 339, 1713, 0, 1713, 0.18, None, None, 308.34, 1713, 998599, location]


def test_the_type_is_read_from_the_registers_own_columns() -> None:
    ew = m.parse_scheme_file("Utkal_EW.xlsx", _xlsx(EW_HEAD, [_ew("SHL27A000217", "T6H49888")]))
    rsa = m.parse_scheme_file("Utkal_RSA.xlsx", _xlsx(RSA_HEAD, [_rsa("RSA27O000028", "T2J78772")]))
    assert (ew.kind, rsa.kind) == ("EW", "RSA")
    assert ew.file_format == "XLSX"
    # a file said to be the other type is refused rather than filed under it
    with pytest.raises(HTTPException) as refused:
        m.parse_scheme_file("x.xlsx", _xlsx(RSA_HEAD, [_rsa("RSA27O000028", "T2J78772")]), "EW")
    assert "RSA" in refused.value.detail


def test_an_ew_row_is_read_with_its_own_tax_not_the_incentives() -> None:
    row = m.parse_scheme_file("ew.xlsx", _xlsx(EW_HEAD, [_ew("SHL27A000217", "t6h49888")])).rows[0]
    assert row["scheme_number"] == "SHL27A000217"
    assert row["scheme_date"] == date(2026, 10, 14)
    assert row["chassis"] == "T6H49888" and row["registration_number"] == "OD02DV8990"
    assert row["customer_code"] == "R270905009"
    assert row["model"] == "XUV 7XO Diesel MT"  # the family without its code, the description being empty
    assert row["channel"] == m.SALES and row["category"] == "PAID-WITHIN WARRANTY"
    assert row["scheme_amount"] == Decimal(19745) and row["tax_amount"] == Decimal("3554.1")
    assert row["cancelled"] is False and row["location"] == "Tamando"


def test_an_rsa_row_takes_the_tax_after_its_amount_and_its_model_description() -> None:
    row = m.parse_scheme_file("rsa.xlsx", _xlsx(RSA_HEAD, [_rsa("RSA27O000028", "T2J78772")])).rows[0]
    assert row["scheme"] == "RSA Scheme for 4th Year"  # the non-breaking space is dropped
    assert row["model"] == "XUV3XO AX5 PM MT RP"
    assert row["scheme_amount"] == Decimal(1713) and row["tax_amount"] == Decimal("308.34")


def test_who_sold_it_from_sold_by_else_the_designation() -> None:
    assert m.channel_of("SALS", None) == m.SALES
    assert m.channel_of("SERV", "Field Sales Consultant") == m.SERVICE
    # the RSA register leaves Sold By empty for the workshop's entries
    assert m.channel_of(None, "Service Manager") == m.SERVICE
    assert m.channel_of("", "Bodyshop Manager ") == m.SERVICE
    assert m.channel_of(None, "SC-Trainee-Personal") == m.SALES
    assert m.channel_of(None, None) == m.UNKNOWN


def test_repeats_blank_lines_and_a_header_below_a_title_row() -> None:
    data = _xlsx(RSA_HEAD, [
        _rsa("RSA27O000028", "T2J78772"),
        _rsa("RSA27O000028", "T2J78772"),  # pasted a second time
        _rsa("RSA27O000029", "T5J25790", location="Puri By Pass"),
        [None] * len(RSA_HEAD),
        [None] * 25 + [3426] + [None] * 7,  # a total line: no vehicle
    ], lead=[["All RSA Punched in October"], []])
    parsed = m.parse_scheme_file("Shivnath_RSA.xlsx", data)
    assert len(parsed.rows) == 2 and parsed.duplicates == 1
    assert parsed.locations == ["Puri By Pass"]
    assert m.period_of(parsed.rows) == date(2026, 10, 1)


def test_a_date_excel_left_as_a_number_and_a_cancelled_registration() -> None:
    assert m.scheme_day(46296.0) == date(2026, 10, 1)
    assert m.scheme_day("14-Oct-2026") == date(2026, 10, 14)
    assert m.scheme_day("") is None and m.scheme_day(11) is None
    parsed = m.parse_scheme_file("ew.xlsx", _xlsx(EW_HEAD, [
        _ew("SHL1", "T6H49888", when=46309.0, cancelled=datetime(2026, 10, 20)),
        _ew("SHL2", "T6H49889", status="Cancelled"),
    ]))
    assert [r["cancelled"] for r in parsed.rows] == [True, True]
    assert parsed.rows[0]["scheme_date"] == date(2026, 10, 14) and parsed.rows[0]["cancelled_on"] == date(2026, 10, 20)


def test_a_csv_dump_and_what_is_not_a_scheme_dump() -> None:
    lines = [",".join(RSA_HEAD), ",".join(str(v) if v is not None else "" for v in
                                         _rsa("RSA27O000028", "T2J78772", when="14-10-2026"))]
    parsed = m.parse_scheme_file("rsa.csv", ("\n".join(lines) + "\n").encode())
    assert parsed.file_format == "CSV" and parsed.kind == "RSA" and parsed.rows[0]["scheme_date"] == date(2026, 10, 14)
    with pytest.raises(HTTPException):
        m.parse_scheme_file("other.xlsx", _xlsx(["Policy No", "VIN", "OD Discount(%)."], [["1", "MA1X", 60]]))
    with pytest.raises(HTTPException):
        m.parse_scheme_file("notes.pdf", b"%PDF-1.7")


def test_keys_the_dumps_are_matched_on() -> None:
    assert registration_key("od 02 dv-8990") == "OD02DV8990"
    assert registration_key("23BH5495F") == "23BH5495F"
    # an unregistered vehicle's chassis in the registration column, an RTO's name, 'New'
    assert registration_key("T6H86068") is None
    assert registration_key("OD02 BHUBANESWAR") is None and registration_key("New") is None
    assert customer_code_key(" r270905009 ") == "R270905009"
    assert customer_code_key("12345") is None and customer_code_key(None) is None


def _index() -> JourneyIndex:
    index = JourneyIndex()
    for jid, delivered in (("j1", True), ("j2", False), ("j3", True), ("j4", True)):
        index.journeys[jid] = {"journeyId": jid, "reference": jid, "outletId": None, "customer": None, "names": set(),
                               "model": "XUV 7XO", "hasDelivery": delivered}
    index.by_chassis["T6H49888"].add("j1")
    index.by_chassis["T2J78772"].update({"j1", "j2"})  # booked twice, one delivered
    index.by_chassis["T5J25790"].update({"j3", "j4"})  # two delivered: not settled
    index.by_customer_code["R270600851"].add("j3")
    index.by_customer_code["R270000001"].update({"j1", "j3"})  # a fleet buyer: not one Journey
    index.by_registration["OD13AC1616"].add("j4")
    return index


def test_a_row_is_matched_on_chassis_then_customer_id_then_registration() -> None:
    index = _index()
    assert m.match_scheme_row(index, {"chassis": "T6H49888"}) == ("j1", "CHASSIS", None)
    # the full VIN matches the short chassis
    assert m.match_scheme_row(index, {"chassis": "MA1NE2ZTFT6H49888"}) == ("j1", "CHASSIS", None)
    assert m.match_scheme_row(index, {"chassis": "T2J78772"}) == ("j1", "CHASSIS", None)
    assert m.match_scheme_row(index, {"chassis": "T9Z00000", "customer_code": "R270600851"}) == ("j3", "CUSTOMER_ID", None)
    assert m.match_scheme_row(index, {"chassis": "T9Z00000", "customer_code": "R270000001",
                                      "registration_number": "OD13AC1616"}) == ("j4", "REGISTRATION", None)
    assert m.match_scheme_row(index, {"chassis": "T9Z00000", "customer_code": "R270000001"}) == (None, None, None)


def test_a_chassis_on_two_delivered_journeys_is_not_guessed() -> None:
    assert m.match_scheme_row(_index(), {"chassis": "T5J25790"}) == (None, None, "SEVERAL_JOURNEYS")
    # ... unless the customer id settles it
    assert m.match_scheme_row(_index(), {"chassis": "T5J25790", "customer_code": "R270600851"}) == ("j3", "CUSTOMER_ID", None)


def _outlets() -> list[dict]:
    def outlet(oid: str, name: str, labels: list[str], anchor: str | None = None) -> dict:
        return {"outlet_id": oid, "outlet_name": f"Aditya Motors - {name}", "dms_outlet_id": anchor or oid,
                "dms_labels": labels, "catch_all": False}

    return [outlet("cut", "Cuttack", ["CUTTACK"]), outlet("jaj", "Jajpur", ["JAJPUR", "JHAJPUR"]),
            outlet("khu", "Khordha", ["KHORDHA"], "cut"), outlet("ken", "Kendrapara", ["KENDRAPARA"], "cut")]


def test_a_dump_naming_its_location_covers_that_dms_locations_outlets_only() -> None:
    outlets = _outlets()
    assert m.covered_outlets([["Jajpur"]], outlets) == {"jaj"}
    # Aditya's other file: every outlet standing under CUTTACK in the DMS
    assert m.covered_outlets([["Cuttack"]], outlets) == {"cut", "khu", "ken"}
    assert m.covered_outlets([["Jajpur"], ["Cuttack"]], outlets) == {"cut", "jaj", "khu", "ken"}
    # no Location in the file, or one no outlet goes by: the whole dealership
    assert m.covered_outlets([[]], outlets) is None
    assert m.covered_outlets([["Jajpur"], ["Somewhere Else"]], outlets) is None
    assert m.unmapped_locations(["Jajpur", "Somewhere Else"], outlets) == ["Somewhere Else"]


def _row(**over) -> dict:
    return {"cancelled": False, "journey_id": "j1", "channel": m.SALES, "ew_sold": True, "rsa_sold": False, **over}


def test_where_a_dump_row_stands() -> None:
    assert m.classify(_row(), "EW") == m.IN_SYNC
    assert m.classify(_row(), "RSA") == m.NOT_BOUGHT
    assert m.classify(_row(journey_id=None), "EW") == m.NOT_IN_VERIGENCE
    assert m.classify(_row(journey_id=None, channel=m.UNKNOWN), "EW") == m.NOT_IN_VERIGENCE
    # the workshop's scheme on an older vehicle: no Journey is expected
    assert m.classify(_row(journey_id=None, channel=m.SERVICE), "EW") == m.SERVICE_ONLY
    # ... but one that is on a Journey is read like any other
    assert m.classify(_row(channel=m.SERVICE), "EW") == m.IN_SYNC
    assert m.classify(_row(cancelled=True), "EW") == m.CANCELLED


def test_the_day_a_journeys_sale_belongs_to() -> None:
    assert m.journey_day({"invoice_date": "2026-10-13", "delivery_date": "2026-10-15"}) == date(2026, 10, 13)
    assert m.journey_day({"invoice_date": None, "delivery_date": "2026-10-15"}) == date(2026, 10, 15)
    assert m.journey_day({"invoice_date": "13/10/2026", "delivery_started": date(2026, 10, 16)}) == date(2026, 10, 16)
    assert m.journey_day({}) is None


def _item(outcome: str, **over) -> dict:
    return {"kind": "EW", "dealerName": "Utkal", "outcome": outcome, "journeyReference": "UTK-1", "chassis": "T6H49888",
            **over}


def test_the_excel_has_a_summary_and_a_sheet_per_list_with_the_readers_note() -> None:
    dealer = {"dealerId": "d", "dealerName": "Utkal", "uploaded": True, "coverage": None, "rows": 5, "sales": 3,
              "service": 2, "cancelled": 1, "matched": 2, "inSync": 1, "notBought": 1, "papersPending": 1,
              "notInDump": 1, "notInVerigence": 1, "serviceOnly": 1}
    report = {
        "month": "2026-10", "kind": "EW", "kindLabel": "Extended warranty", "dealers": [dealer],
        "inSync": [_item(m.IN_SYNC, foc=True, focCharged=True)],
        "notBought": [_item(m.NOT_BOUGHT, papersOnFile=False)],
        "notInDump": [_item(m.NOT_IN_DUMP, journeyDate="2026-10-30", monthEnd=True)],
        "notInVerigence": [_item(m.NOT_IN_VERIGENCE, severalJourneys=True)],
        "serviceOnly": [_item(m.SERVICE_ONLY)], "cancelled": [_item(m.CANCELLED, cancelledOn="2026-10-20")],
    }
    empty = {**report, "kind": "RSA", "kindLabel": "RSA", "dealers": [{**dealer, "uploaded": False, "rows": 0}],
             "inSync": [], "notBought": [], "notInDump": [], "notInVerigence": [], "serviceOnly": [], "cancelled": []}
    wb = load_workbook(io.BytesIO(m.build_scheme_excel([report, empty])))
    assert wb.sheetnames == ["Summary", "In sync", "Not bought per Verigence", "Bought, not in dump", "Not in Verigence",
                             "Sold by service", "Cancelled"]
    summary = [[c.value for c in row] for row in wb["Summary"].iter_rows(min_row=3)]
    assert summary[0][:5] == ["Extended warranty", "Utkal", "Yes", "All", 5]
    assert summary[1][:4] == ["RSA", "Utkal", "No", None]

    def note(sheet: str) -> str:
        return [c.value for c in wb[sheet][2]][-1]

    assert "charged to the customer" in note("In sync")
    assert "not on file yet" in note("Not bought per Verigence")
    assert "next month" in note("Bought, not in dump")
    assert "More than one Journey" in note("Not in Verigence")
    assert note("Cancelled") == "Cancelled on 2026-10-20."


def test_a_free_scheme_with_no_charge_is_explained_not_flagged_as_missing_papers() -> None:
    assert m._note(_item(m.NOT_BOUGHT, foc=True, papersOnFile=False)) == (
        "Free of cost (FOC) in the dump: no charge is expected.")
    assert "shows no such charge" in m._note(_item(m.NOT_BOUGHT, papersOnFile=True))
    assert "cancelled in the dump on 2026-10-20" in m._note(
        _item(m.NOT_IN_DUMP, cancelledInDump=True, cancelledOn="2026-10-20"))
