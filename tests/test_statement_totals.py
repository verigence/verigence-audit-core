"""The NDC's and the cost sheet's rows add up to the totals they print
(product owner 2026-11-05). Real Postgres for the task."""
from __future__ import annotations

import json
from decimal import Decimal as D
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_evidence,
    add_extracted_field,
    add_page,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_account_statement import read_statement
from audit_core.uc03_statement_totals import (
    TASK_TYPE,
    cost_sheet_differences,
    describe,
    statement_differences,
    sync_statement_totals,
)

# Utkal's Vehicle Account Statement (UTK-RM-261104-002's figures).
UTKAL = [
    {"particulars_raw": "Vehicle cost", "debit_amount": "852000"},
    {"particulars_raw": "Registration", "debit_amount": "3685"},
    {"particulars_raw": "Permit", "debit_amount": "6500"},
    {"particulars_raw": "Accessories", "debit_amount": "5140"},
    {"particulars_raw": "RSA", "debit_amount": "940"},
    {"particulars_raw": "Finance D/O KJR/2627/133", "credit_amount": "741090"},
    {"particulars_raw": "MR No KRM/2627/3592", "credit_amount": "11000"},
    {"particulars_raw": "MR No KRM/2627/3677", "credit_amount": "100000"},
    {"particulars_raw": "Scheme Discount", "credit_amount": "13000"},
    {"particulars_raw": "Other Discount (if any)", "credit_amount": "7000"},
]
UTKAL_PRINTED = {"total_cost": "868265", "total_receipts": "872090"}


def _fields(rows, **printed):
    return {"line_items": json.dumps(rows), **printed}


def test_a_statement_whose_rows_give_its_totals_adds_up():
    assert statement_differences(_fields(UTKAL, **UTKAL_PRINTED)) == []


def test_a_row_misread_or_read_twice_is_found_against_each_total():
    misread = [dict(r) for r in UTKAL]
    misread[0]["debit_amount"] = "825000"  # a digit swapped
    [cost] = statement_differences(_fields(misread, **UTKAL_PRINTED))
    assert (cost["total"], cost["printed"], cost["rows"], cost["difference"]) == (
        "Total Cost", D(868265), D(841265), D(27000))
    # A receipt read twice: Total Receipts no longer adds up (shown against the nearest way the
    # rows add up: the receipts with the discounts, Rs 9,52,090).
    twice = [*UTKAL, {"particulars_raw": "MR No KRM/2627/3677", "credit_amount": "100000"}]
    found = statement_differences(_fields(twice, **UTKAL_PRINTED))
    assert [(d["total"], d["rows"]) for d in found] == [("Total Receipts", D(952090))]


def test_a_form_may_print_its_discounts_on_either_side():
    """Aditya's Net Receivable is the charges less the offers printed among them;
    its cash discount stands with the receipts (ADI-RD-261101-027)."""
    aditya = [
        {"particulars_raw": "EX-SHOW ROOM PRICE OF VEHICLE", "debit_amount": "1327501"},
        {"particulars_raw": "LESS: CONSUMER OFFER (IF APPLICABLE)", "credit_amount": "25000"},
        {"particulars_raw": "LESS: CORPORATE OFFER (IF APPLICABLE)", "credit_amount": "5000"},
        {"particulars_raw": "TCS ON INVOICE", "debit_amount": "13275"},
        {"particulars_raw": "INSURANCE COST (NIL DEP)", "debit_amount": "61335"},
        {"particulars_raw": "REGISTRATION COST", "debit_amount": "19491"},
        {"particulars_raw": "EXTENDED WARRANTY", "debit_amount": "22750"},
        {"particulars_raw": "RSA", "debit_amount": "2021"},
        {"particulars_raw": "ACCESSORIES", "debit_amount": "35927"},
        {"particulars_raw": "BOOKING AMOUNT", "credit_amount": "21000"},
        {"particulars_raw": "DEPOSITED INTO BANK", "credit_amount": "1388163"},
        {"particulars_raw": "DEPOSITED INTO BANK", "credit_amount": "30837"},
        {"particulars_raw": "**CASH DISCOUNT", "credit_amount": "12000"},
    ]
    assert statement_differences(_fields(aditya, net_value="1452300", total_receipts="1452000")) == []
    [net] = statement_differences(_fields(aditya, net_value="1458300"))
    assert (net["rows"], net["difference"]) == (D(1457300), D(1000))


def test_a_copy_not_judged():
    assert statement_differences(_fields([], total_cost="100")) is None
    assert statement_differences(_fields(UTKAL)) is None  # prints no total
    tally = [{"particulars_raw": "Vehicle Sale 5%", "debit_amount": "2812850", "voucher_type": "Sales"},
             {"particulars_raw": "ICICI BANK", "credit_amount": "2000000", "running_balance": "812850"},
             {"particulars_raw": "ICICI BANK", "credit_amount": "812850", "running_balance": "0"}]
    assert statement_differences(_fields(tally, total_receipts="2812850")) is None


def test_utkal_exchange_value_is_the_old_vehicle_taken_in_kind():
    """UTK-BP-261104-002: "Exchange Value" Rs 6,70,000 in the payment column,
    counted in the printed Total Receipt, was left unread."""
    read = read_statement([*UTKAL, {"particulars_raw": "Exchange Value", "credit_amount": "670000"}])
    assert read.old_vehicle_value == D(670000) and read.unmapped == []
    assert read.receipts == D(741090 + 11000 + 100000 + 670000)


def test_the_offer_letters_finance_amount_counts_once():
    """SHI-SS-261029-001: the finance amount planned and the bank's payment
    listed among the receipts; SHI-SS-261027-019: the bank's payment not
    listed, so the plan's finance amount is the money."""
    plan = [{"particulars_raw": "EX SHOWROOM (1-2)", "debit_amount": "2784999"},
            {"particulars_raw": "BOOKING AMOUNT", "credit_amount": "70000"},
            {"particulars_raw": "FINANCE AMOUNT", "credit_amount": "2000000"},
            {"particulars_raw": "RECEIPT PB/26-27/0553", "credit_amount": "70000"},
            {"particulars_raw": "RECEIPT PB/26-27/0825", "credit_amount": "744500"}]
    listed = read_statement([*plan, {"particulars_raw": "RECEIPT PB/26-27/0826", "credit_amount": "2000000"}])
    assert listed.receipts == D(2814500) and listed.financer == D(2000000)
    assert read_statement(plan).receipts == D(2814500)
    # Receipts listed without their number ("RECEIPT MR-") are the letter's receipts all the same.
    unnumbered = read_statement([{"particulars_raw": "VEHICLE PRICE", "debit_amount": "1797000"},
                                 {"particulars_raw": "BOOKING AMOUNT", "credit_amount": "21000"},
                                 {"particulars_raw": "FINANCE AMOUNT", "credit_amount": "1927182"},
                                 {"particulars_raw": "RECEIPT MR-", "credit_amount": "21000"},
                                 {"particulars_raw": "RECEIPT MR-", "credit_amount": "1927182"}])
    assert unnumbered.receipts == D(1948182)


def test_a_cost_sheet_is_judged_once_its_rows_are_read():
    rows = [{"particulars_raw": "Ex Showroom Price", "amount": "1091500"},
            {"particulars_raw": "TCS@1", "amount": "10915"},
            {"particulars_raw": "Insurance", "amount": "32500"},
            {"particulars_raw": "RTO", "amount": "12000"},
            {"particulars_raw": "On Road Price", "amount": "1146915"}]
    assert cost_sheet_differences({"total_on_road_price": "1146915", "line_items": json.dumps(rows)}) == []
    # SHI-YK-261102-002's quotation as printed: its rows add up to Rs 11,46,915, its on-road price
    # says Rs 11,49,436.
    [found] = cost_sheet_differences({"total_on_road_price": "1149436", "line_items": json.dumps(rows)})
    assert found["difference"] == D(2521)
    with_discount = [*rows[:4], {"particulars_raw": "Less Discount", "amount": "20000", "deduction": True}]
    assert cost_sheet_differences({"total_on_road_price": "1126915", "line_items": json.dumps(with_discount)}) == []
    # Read before its rows were (four figures only): not judged.
    assert cost_sheet_differences({"total_on_road_price": "1149436", "ex_showroom_price": "1091500"}) is None


def test_the_task_text_names_each_copy_and_total():
    text_ = describe([{"document": "NDC", "pageNumber": 3, "differences": [
        {"total": "Total Cost", "printed": D(1024800), "rows": D(1023600), "difference": D(1200)}]}])
    assert text_ == "NDC (page 3): Total Cost printed Rs 10,24,800, its rows add up to Rs 10,23,600 (Rs 1,200 apart)"


# ── on the database ──────────────────────────────────────────────────────────
@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="addsup")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _ndc(journey, rows, printed, *, page=4):
    document_id = uuid4()
    add_page(journey, page_number=page, status="READY", di_document_id=document_id)
    add_evidence(journey, di_document_id=document_id, document_type_key="customer_ledger")
    for key, value in {"line_items": json.dumps(rows), **printed}.items():
        add_extracted_field(journey, di_document_id=document_id, field_key=key, value=value,
                            document_type="customer_ledger")
    return document_id


def _sync(journey):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return sync_statement_totals(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _task(journey):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        row = c.execute(
            text("SELECT task_status, severity, title, description, assigned_role_code FROM auditcore.p2_tasks "
                 "WHERE tenant_id=:t AND journey_id=:j AND task_type=:k"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": TASK_TYPE}).mappings().one_or_none()
    return dict(row) if row else None


def test_a_copy_that_does_not_add_up_raises_one_high_task_that_closes_when_corrected(journey):
    misread = [dict(r) for r in UTKAL]
    misread[0]["debit_amount"] = "825000"
    document_id = _ndc(journey, misread, UTKAL_PRINTED)
    assert _sync(journey) == {"judged": 1, "copies": 1}
    task = _task(journey)
    assert (task["severity"], task["assigned_role_code"]) == ("HIGH", "PC")
    assert "NDC (page 4): Total Cost printed Rs 8,68,265, its rows add up to Rs 8,41,265" in task["description"]
    # The PC corrects the reading: the task closes by itself.
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.journey_document_extracted_fields SET effective_value=CAST(:v AS jsonb), "
                       "is_modified=true WHERE tenant_id=:t AND di_document_id=:d AND field_key='line_items'"),
                  {"v": json.dumps(json.dumps(UTKAL)), "t": journey.tenant_id, "d": document_id})
    assert _sync(journey) == {"judged": 1, "copies": 0}
    assert _task(journey)["task_status"] == "VERIFIED_COMPLETE"


def test_the_pc_can_close_it_when_the_dealers_sum_is_wrong():
    from audit_core.uc03_p2_tasks import CLOSE_TASK_TYPES

    assert TASK_TYPE in CLOSE_TASK_TYPES


def test_the_cost_sheets_titled_as_statements_are_found(journey):
    from audit_core.uc03_cost_sheet_statements import statement_pages

    for page, title in ((16, "FINAL VEHICLE ACCOUNTS STATEMENT"), (17, "QUOTATION")):
        document_id = uuid4()
        add_page(journey, page_number=page, status="READY", di_document_id=document_id)
        add_evidence(journey, di_document_id=document_id, document_type_key="cost_sheet")
        add_extracted_field(journey, di_document_id=document_id, field_key="document_title", value=title,
                            document_type="cost_sheet")
    [found] = statement_pages(journey.engine, journey.tenant_id)
    assert found["page_number"] == 16 and found["title"] == "FINAL VEHICLE ACCOUNTS STATEMENT"
