"""Several copies of the NDC and the cost sheet (product owner, 2026-11-02).

Every copy on file is read and copies are never added together. The latest
dated copy that prints the vehicle line gives every figure it prints; another
page (no vehicle line) adds only what that copy does not print; an older full
copy is only compared. Copies that differ by more than Rs 100, or a figure the
latest copy no longer prints, raise an information task for the PC. With one
copy everything is as before.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import add_extracted_field, create_p2_journey, database_engine
from sqlalchemy import text
from test_uc03_account_statement import (
    UTKAL,
    _cost_sheet,
    _ledger,
    _lines,
    _set_policy,
    _sync,
)

from audit_core import uc03_account_statement as module
from audit_core.db import set_tenant_context
from audit_core.uc03_deal_source_policy import POLICY_ACCOUNT_STATEMENT

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="copies")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _doc(rows: list[dict], *, day: str | None = None, minutes: int = 0) -> tuple[dict, list[dict]]:
    fields = {"invoice_date": day} if day else {}
    return ({"documentId": uuid4(), "evidenceId": uuid4(), "linkedAtUtc": NOW + timedelta(minutes=minutes),
             "fields": fields}, rows)


VEHICLE = {"particulars_raw": "Vehicle cost", "debit_amount": 1000000}


def test_one_copy_reads_exactly_as_before() -> None:
    combined, copies = module.combine_statements([_doc(UTKAL)])
    alone = module.read_statement(UTKAL)
    assert combined == alone and copies.differences == []


def test_the_latest_dated_full_copy_gives_the_figures_and_an_older_one_is_compared() -> None:
    offer = _doc([VEHICLE, {"particulars_raw": "Insurance", "debit_amount": 40000},
                  {"particulars_raw": "Other Discount (if any)", "credit_amount": 5000}], day="2026-10-01", minutes=50)
    final = _doc([VEHICLE, {"particulars_raw": "Insurance", "debit_amount": 42000}], day="2026-10-20")
    read, copies = module.combine_statements([offer, final])
    # the final statement is dated later, though it was linked first
    assert copies.primary is final[0]
    assert read.components == {"ex_showroom_price": D(1000000), "insurance_amount": D(42000)}
    # the discount only the offer letter printed is not taken, but listed
    assert read.discounts == {}
    assert {(d["key"], d["taken"], d["other"]) for d in copies.differences} == {
        ("insurance_amount", D(42000), D(40000)), ("ADDITIONAL_DISCOUNT", None, D(5000))}


def test_another_page_adds_what_the_statement_does_not_print() -> None:
    page_one = _doc([VEHICLE, {"particulars_raw": "Insurance", "debit_amount": 40000}], minutes=1)
    page_two = _doc([{"particulars_raw": "Other Discount (if any)", "credit_amount": 5000},
                     {"particulars_raw": "Finance D/O", "credit_amount": 900000}], minutes=2)
    read, copies = module.combine_statements([page_one, page_two])
    assert copies.primary is page_one[0]
    assert read.discounts == {"ADDITIONAL_DISCOUNT": D(5000)} and read.financer == D(900000)
    assert copies.origin[("DISCOUNT", "ADDITIONAL_DISCOUNT")] is page_two[0]
    assert copies.differences == []


def test_the_same_page_on_file_twice_is_one_copy() -> None:
    read, copies = module.combine_statements([_doc(UTKAL, minutes=1), _doc(UTKAL, minutes=2)])
    assert read == module.read_statement(UTKAL) and copies.differences == []


def test_tally_pages_are_read_once_each() -> None:
    row_a = {"entry_date": "2026-10-01", "particulars_raw": "Vehicle Sales 28%", "debit_amount": 1000000}
    row_b = {"entry_date": "2026-10-02", "particulars_raw": "HDFC Bank (Finance DO)", "credit_amount": 800000}
    rows = module.combined_tally_rows([(_doc([])[0], [row_a, row_b]), (_doc([])[0], [row_a])])
    assert rows == [row_a, row_b]


def test_cost_sheet_copies_follow_the_same_rule() -> None:
    older = {"documentId": uuid4(), "evidenceId": uuid4(), "linkedAtUtc": NOW,
             "fields": {"cost_sheet_date": "2026-10-01", "ex_showroom_price": "1000000", "insurance_amount": "40000"}}
    newer = {"documentId": uuid4(), "evidenceId": uuid4(), "linkedAtUtc": NOW,
             "fields": {"cost_sheet_date": "2026-10-10", "ex_showroom_price": "1000000", "insurance_amount": "41000"}}
    blank = {"documentId": uuid4(), "evidenceId": uuid4(), "linkedAtUtc": NOW + timedelta(hours=1), "fields": {}}
    sheet, copies = module.combine_cost_sheets([older, newer, blank])
    assert copies.primary is newer and sheet.components["insurance_amount"] == D(41000)
    assert [(d["key"], d["other"]) for d in copies.differences] == [("insurance_amount", D(40000))]


# ── the sync and the Deal ────────────────────────────────────────────────────
def _task(journey, task_type: str) -> dict | None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(
            text("SELECT task_status, severity, description FROM auditcore.p2_tasks "
                 "WHERE tenant_id=:t AND journey_id=:j AND task_type=:type"),
            {"t": journey.tenant_id, "j": journey.journey_id, "type": task_type},
        ).mappings().one_or_none()


def _date(journey, document_id, day: str) -> None:
    add_extracted_field(journey, di_document_id=document_id, field_key="invoice_date", value=day,
                        document_type="customer_ledger")


def test_two_copies_on_file_feed_the_deal_from_the_latest_and_raise_the_task(journey) -> None:
    from audit_core.uc03_p2_journey360 import deal

    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    final_id, _ = _ledger(journey, UTKAL)
    _date(journey, final_id, "2026-10-20")
    offer = [row if row["particulars_raw"] != "Insurance amount" else {**row, "debit_amount": 35000} for row in UTKAL]
    offer_id, _ = _ledger(journey, offer)
    _date(journey, offer_id, "2026-10-01")
    _sync(journey)
    lines = _lines(journey)
    assert lines["insurance_amount"] == (D(38201), f"customer_ledger:{final_id}")
    task = _task(journey, module.COPIES_DIFFERENCE_TASK)
    assert task is not None and task["severity"] == "HIGH" and "Rs 35,000" in task["description"]
    # The older copy's figure is never a second statement figure on the Deal.
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, "
                       "component_key, source_document_type, amount, source_document_id) "
                       "VALUES (:t, :j, 'COMMERCIAL', 'insurance_amount', 'customer_ledger', 35000, :d)"),
                  {"t": journey.tenant_id, "j": journey.journey_id, "d": offer_id})
        view = deal(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    row = next(r for g in view["categories"] for r in g["components"] if r["key"] == "insurance_amount")
    assert D(row["billed"]) == D(38201) and "INVOICES_DISAGREE" not in row["flags"]


def test_copies_that_agree_raise_nothing(journey) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _ledger(journey, UTKAL)
    _ledger(journey, UTKAL)
    _sync(journey)
    assert _task(journey, module.COPIES_DIFFERENCE_TASK) is None


def test_a_second_cost_sheet_is_compared_too(journey) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _cost_sheet(journey, {"cost_sheet_date": "2026-10-01", "ex_showroom_price": "1000000"})
    _cost_sheet(journey, {"cost_sheet_date": "2026-10-05", "ex_showroom_price": "1010000"})
    _sync(journey)
    task = _task(journey, module.COPIES_DIFFERENCE_TASK)
    assert task is not None and "Cost sheet" in task["description"]
