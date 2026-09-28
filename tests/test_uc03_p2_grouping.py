"""Page grouping for multi-document PDFs, checked against the page layouts of
real dealer booking/delivery packets (types only; no customer data)."""
from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter

from audit_core.uc03_p2_grouping import (
    PageFact,
    PlannedDocument,
    merge_pdf_pages,
    needs_document_upload,
    plan_documents,
    read_once_di_types,
)
from audit_core.uc03_p2_registry import get_registry

BF, RC, AA, PAN, UPI = "booking_form", "dealer_receipt", "aadhaar", "pan_card", "upi_screenshot"


def _plan(types, statuses=None):
    pages = [
        PageFact(page_number=index, di_type=di_type,
                 status=(statuses or {}).get(index, "EXTRACTING" if di_type else "SUPPORTING"))
        for index, di_type in enumerate(types, start=1)
    ]
    return [(d.template_key, d.page_numbers) for d in plan_documents(pages, get_registry())]


def test_individual_booking_packet_with_split_aadhaar():
    # Commitment form, OTF, receipt, bank statement, Aadhaar front, Aadhaar back, PAN, employer letter
    plan = _plan([BF, BF, RC, "bank_statement_extract", AA, AA, PAN, None])
    assert plan == [
        ("booking_docket", (1, 2)),
        ("dealer_receipt", (3,)),
        ("bank_statement_extract", (4,)),
        ("aadhaar", (5, 6)),
        ("pan_card", (7,)),
        ("supporting_document", (8,)),
    ]


def test_three_page_gst_certificate_does_not_swallow_a_trailing_unrelated_page():
    plan = _plan([BF, BF, RC, AA, PAN, "gst_certificate", "gst_certificate", "gst_certificate", None])
    assert ("gst_certificate", (6, 7, 8)) in plan
    assert ("supporting_document", (9,)) in plan


def test_unclassified_annexure_joins_only_when_sandwiched():
    plan = _plan(["gst_certificate", None, "gst_certificate", None])
    assert plan == [("gst_certificate", (1, 2, 3)), ("supporting_document", (4,))]


def test_upi_proof_and_receipt_stay_separate():
    plan = _plan([BF, BF, AA, PAN, UPI, RC])
    assert ("upi_screenshot", (5,)) in plan and ("dealer_receipt", (6,)) in plan
    assert len(plan) == 5


def test_corporate_packet_keeps_company_and_individual_pan_apart():
    plan = _plan([BF, BF, RC, PAN, "corporate_id", PAN, AA, None])
    assert ("pan_card", (4,)) in plan and ("pan_card", (6,)) in plan
    assert ("corporate_id", (5,)) in plan


def test_full_delivery_packet():
    types = [
        BF, "cost_sheet", None, "insurance_cover", "rto_challan", "gate_pass", None, None,
        "scrappage_certificate_of_deposit", None, BF, RC, RC, RC, RC, RC, PAN, AA, None, PAN, AA,
        None, None, None, None, "customer_invoice_dms", None, "credit_note", RC,
        "delivery_order_cover", "delivery_order_cover", "accessory_invoice_dms",
    ]
    plan = _plan(types)
    assert ("booking_docket", (1, 11)) in plan  # forms far apart in the scan still form one docket
    assert [pages for key, pages in plan if key == "dealer_receipt"] == [(12,), (13,), (14,), (15,), (16,), (29,)]
    assert ("delivery_order_cover", (30, 31)) in plan
    assert ("cost_sheet", (2,)) in plan and ("supporting_document", (3,)) in plan
    assert sorted(p for _, pages in plan for p in pages) == list(range(1, 33))  # every page accounted for


def test_docket_cover_sheets_are_supporting_and_single_form_docket_is_fine():
    plan = _plan([None, None, BF, AA, PAN, RC])
    assert plan[:3] == [
        ("supporting_document", (1,)),
        ("supporting_document", (2,)),
        ("booking_docket", (3,)),
    ]


def test_failed_pages_stand_alone_and_part_limits_hold():
    plan = _plan([AA, AA, AA], statuses={2: "FAILED"})
    assert ("aadhaar", (1,)) in plan and ("aadhaar", (3,)) in plan
    assert ("supporting_document", (2,)) in plan
    assert _plan([AA, AA, AA]) == [("aadhaar", (1, 2)), ("aadhaar", (3,))]


def _one_page_pdf(width: int) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_merge_keeps_page_order():
    merged = merge_pdf_pages([_one_page_pdf(100), _one_page_pdf(200), _one_page_pdf(300)])
    widths = [float(page.mediabox.width) for page in PdfReader(io.BytesIO(merged)).pages]
    assert widths == [100.0, 200.0, 300.0]


def test_only_always_merged_types_are_read_once():
    read_once = read_once_di_types(get_registry())
    assert {BF, AA, "customer_invoice_dms", "vehicle_rc"} <= read_once
    # receipts and PAN are single-page documents: read page by page
    assert RC not in read_once and PAN not in read_once


def test_a_single_page_of_an_always_merged_type_still_gets_one_upload():
    read_once = read_once_di_types(get_registry())
    assert needs_document_upload(PlannedDocument("booking_docket", BF, (1,)), read_once)
    assert needs_document_upload(PlannedDocument("dealer_receipt", RC, (2, 3)), read_once)
    assert not needs_document_upload(PlannedDocument("dealer_receipt", RC, (2,)), read_once)
    assert not needs_document_upload(PlannedDocument("supporting_document", None, (4,)), read_once)
