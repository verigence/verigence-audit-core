"""The DMS report as one styled document, Excel and PDF (product owner 2026-11-22): the facts in
one report, deliveries grouped as the DMS groups outlets, against the gate register."""
from __future__ import annotations

import io

from openpyxl import load_workbook
from pypdf import PdfReader

from audit_core.uc03_p2_dms_report_document import (
    build_report_pdf,
    build_report_xlsx,
    exception_lists,
    location_rows,
)

NOT_UPLOADED = "Booking on file, delivery not uploaded"


def _figures(**kw: int) -> dict[str, int]:
    base = {k: 0 for k in ("bookingDms", "bookingSynced", "bookingNotInVerigence", "bookingInVerigence",
                           "bookingNotInDms", "deliveryDump", "deliverySynced", "deliveryNotUploaded",
                           "deliveryNotInVerigence", "deliveryInVerigence", "deliveryNotInDms", "deliveryGap",
                           "gateRegister", "registersMissed", "countsPending", "dumpInscope", "dumpOutscope")}
    return {**base, **kw}


def _report() -> dict:
    row = {"dealerName": "Dealer One", "location": "Town A", "eventDate": "2026-11-20", "customerName": "Customer A",
           "mobile": "9000000001", "otf": "B-1", "chassis": "T0A00001", "model": "Model X", "consultant": "SC One",
           "journeyReference": None, "dealType": "Inscope", "intimation": "Not intimated to PC"}
    return {
        "date": "2026-11-21", "month": "2026-11", "generatedBy": "Reviewer One",
        "generatedAtUtc": "2026-11-21T12:35:18+00:00", "rebuilt": {"atUtc": "2026-11-22T03:00:00+00:00"},
        "figures": _figures(bookingDms=10, bookingSynced=8, bookingNotInVerigence=2, deliveryDump=6,
                            deliverySynced=3, deliveryNotUploaded=2, deliveryNotInVerigence=1, gateRegister=7),
        "dealers": [{
            "dealerName": "Dealer One", "bookingDumpDate": "2026-11-21", "deliveryDumpDate": "2026-11-21",
            "figures": _figures(deliveryDump=6, gateRegister=7),
            "locations": [
                {"name": "Town A", "figures": _figures(deliveryDump=5, deliverySynced=3, gateRegister=6),
                 "outlets": [{"outletName": "Town A", "figures": _figures(gateRegister=4, deliveryInVerigence=3)},
                             {"outletName": "Satellite B", "figures": _figures(gateRegister=2)}]},
                {"name": "Town C", "figures": _figures(deliveryDump=1, gateRegister=1),
                 "outlets": [{"outletName": "Town C", "figures": _figures(gateRegister=1)}]},
            ],
            "unmapped": [],
        }],
        "statementLines": ["Dealer One", "* Onsite - gate register missing: none"],
        "rows": [{**row, "kind": "BOOKING", "inVerigence": "Not in Verigence"},
                 {**row, "kind": "DELIVERY", "inVerigence": "Not in Verigence"},
                 {**row, "kind": "DELIVERY", "inVerigence": NOT_UPLOADED, "journeyReference": "J-1"}],
        "nameDiffers": [], "notInDump": [], "deliveryScope": [row], "intimations": [],
    }


def test_deliveries_are_grouped_as_the_dms_groups_outlets():
    rows = location_rows(_report()["dealers"][0])
    assert [(r["kind"], r["label"]) for r in rows] == [
        ("location", "Town A (DMS location, 2 outlets)"), ("outlet", "Town A"), ("outlet", "Satellite B"),
        ("location", "Town C"), ("total", "Dealer One total")]
    # A one-outlet location is one row; its outlet's register is in the location's figures.
    assert rows[3]["figures"]["gateRegister"] == 1


def test_the_exceptions_split_not_in_verigence_from_delivery_not_uploaded():
    lists = {title: rows for title, _, rows, _ in exception_lists(_report())}
    assert len(lists["Bookings not in Verigence"]) == 1
    assert len(lists["Deliveries not in Verigence"]) == 1
    assert [r["journeyReference"] for r in lists["Delivery not uploaded"]] == ["J-1"]


def test_the_excel_holds_every_fact_styled():
    book = load_workbook(io.BytesIO(build_report_xlsx(_report())))
    assert book.sheetnames == ["Summary", "By DMS location", "Statement", "Bookings not in Verigence",
                               "Deliveries not in Verigence", "Delivery not uploaded", "Matched, name differs",
                               "In Verigence, not in dump", "Delivery dump scope", "PC intimations",
                               "How it is counted"]
    grouping = book["By DMS location"]
    labels = [grouping.cell(row=r, column=1).value for r in range(1, grouping.max_row + 1)]
    outlet = labels.index("Satellite B") + 1
    assert grouping.row_dimensions[outlet].outline_level == 1  # folds under its DMS location
    assert grouping.cell(row=outlet, column=5).value is None   # the DMS's figures are the location's
    assert grouping.cell(row=outlet, column=2).value == 2      # its own gate register
    assert book["Bookings not in Verigence"].tables          # a filterable Excel table
    assert book["Summary"]["A1"].value == "DMS Reconciliation Report"


def test_the_pdf_reads_as_one_report():
    pdf = PdfReader(io.BytesIO(build_report_pdf(_report())))
    text = " ".join(page.extract_text() for page in pdf.pages)
    assert len(pdf.pages) >= 4
    for words in ("DMS Reconciliation Report", "Deliveries by DMS location", "Satellite B", "Statement",
                  "Bookings not in Verigence", "Delivery not uploaded", f"Page 1 of {len(pdf.pages)}"):
        assert words in text
