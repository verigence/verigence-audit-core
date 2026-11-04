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


def test_of_a_quotation_and_the_final_cost_sheet_the_one_the_proof_documents_agree_with_stands() -> None:
    """16 journeys, 2026-11-04 (UTK-JP-261101-014): a quotation pricing the car
    at Rs 9,96,500 with RTO 87,264 (dated "2024-09-15", a misread) and the
    final sheet at Rs 9,78,501 with RTO 6,816. The invoice prints 9,96,500
    gross and 9,78,501 net, the challan 6,816: the final sheet agrees on two
    figures, the quotation on one. With no proof at all the newest stands."""
    quotation = {"documentId": uuid4(), "evidenceId": uuid4(), "linkedAtUtc": NOW,
                 "fields": {"cost_sheet_date": "2026-10-06", "ex_showroom_price": "996500", "rto_amount": "87264"}}
    final = {"documentId": uuid4(), "evidenceId": uuid4(), "linkedAtUtc": NOW,
             "fields": {"cost_sheet_date": "2024-09-15", "ex_showroom_price": "978501", "rto_amount": "6816"}}
    proof = {"ex_showroom_price": (D("996500.01"), D(978501)), "registration_charges": (D(6816),),
             "insurance_amount": ()}
    sheet, copies = module.combine_cost_sheets([quotation, final], proof=proof)
    assert copies.primary is final and sheet.components["ex_showroom_price"] == D(978501)
    assert sheet.components["registration_charges"] == D(6816)
    # a tie (one figure each) leaves the newest copy standing, as with no proof
    tie = {**proof, "registration_charges": ()}
    assert module.combine_cost_sheets([quotation, final], proof=tie)[1].primary is quotation
    assert module.combine_cost_sheets([quotation, final])[1].primary is quotation


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


# ── a new reading replaces only a reading of the same page ───────────────────
def _requirement(journey, key: str):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(
            text("INSERT INTO auditcore.journey_document_requirements (tenant_id, journey_id, requirement_key, "
                 "document_type_key, process_area, requirement_level) VALUES (:t, :j, :k, :k, 'DELIVERY', "
                 "'REQUIRED') RETURNING journey_document_requirement_id"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": key}).scalar_one()


def _copy_on_file(journey, requirement, *, batch, page: int, sha: str, group: bool = False):
    document_id = uuid4()
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id, "
                       "di_document_id, journey_document_requirement_id, document_type_key, evidence_purpose, "
                       "process_area, association_status) VALUES (:t, :j, :c, :s, :d, :r, 'customer_ledger', "
                       "'DELIVERY_DOCUMENT', 'DELIVERY', 'ACTIVE')"),
                  {"t": journey.tenant_id, "j": journey.journey_id, "c": journey.customer_id, "s": uuid4(),
                   "d": document_id, "r": requirement})
        _queue(c, journey, batch=batch, page=page, sha=sha, document_id=document_id, group=group)
    return document_id


def _queue(c, journey, *, batch, page: int, sha: str, document_id, group: bool = False) -> None:
    # A page read again is a unit of its own over the same page (unit_kind GROUP).
    c.execute(text("INSERT INTO auditcore.p2_document_queue (tenant_id, batch_id, journey_id, page_number, "
                   "page_numbers, unit_kind, page_sha256, page_object_key, client_upload_id, di_document_id, "
                   "queue_status) VALUES (:t, :b, :j, :p, :ps, :u, :sha, :k, :cu, :d, 'READY')"),
              {"t": journey.tenant_id, "b": batch, "j": journey.journey_id, "p": page, "ps": [page],
               "u": "GROUP" if group else "PAGE", "sha": sha, "k": f"p2/{batch}/{page}",
               "cu": f"p2-{uuid4().hex}", "d": document_id})


def _batch(journey):
    from p2_support import add_page

    batch, _, _ = add_page(journey, page_number=99)
    return batch


def test_only_a_reading_of_the_same_page_is_replaced(journey) -> None:
    from audit_core.uc03_pc_booking_documents import _same_page_copy

    requirement = _requirement(journey, "customer_ledger")
    batch = _batch(journey)
    page_one = _copy_on_file(journey, requirement, batch=batch, page=1, sha="a" * 64)
    _copy_on_file(journey, requirement, batch=batch, page=2, sha="b" * 64)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        reread, other_page, same_image = uuid4(), uuid4(), uuid4()
        _queue(c, journey, batch=batch, page=1, sha="c" * 64, document_id=reread, group=True)
        _queue(c, journey, batch=batch, page=3, sha="d" * 64, document_id=other_page)
        _queue(c, journey, batch=_batch(journey), page=1, sha="a" * 64, document_id=same_image)

        def replaced(document_id):
            evidence = _same_page_copy(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                       requirement_ref=requirement, document_id=document_id)
            if evidence is None:
                return None
            return c.execute(text("SELECT di_document_id FROM auditcore.evidence WHERE tenant_id=:t "
                                  "AND evidence_id=:e"), {"t": journey.tenant_id, "e": evidence}).scalar_one()

        # page 1 read again replaces page 1; page 3 is another page, kept beside;
        # the same page image uploaded again replaces it too; an unknown page is kept
        assert replaced(reread) == page_one
        assert replaced(other_page) is None
        assert replaced(same_image) == page_one
        assert replaced(uuid4()) is None


def test_the_ndc_and_cost_sheet_keep_every_copy() -> None:
    from audit_core.uc03_pc_booking_documents import (
        _COPY_REQUIREMENT_KEYS,
        _is_repeatable_requirement,
    )

    assert {"customer_ledger", "cost_sheet"} <= _COPY_REQUIREMENT_KEYS
    # not repeatable: a late callback for a replaced reading of the same page
    # never brings it back
    assert not _is_repeatable_requirement("customer_ledger")


# ── STATEMENT_COPY_RESTORE ───────────────────────────────────────────────────
def _superseded(journey, document_id) -> None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.evidence SET association_status='SUPERSEDED', "
                       "void_reason='REPLACED_BY_NEWER_UPLOAD' WHERE tenant_id=:t AND di_document_id=:d"),
                  {"t": journey.tenant_id, "d": document_id})


def _status(journey, document_id) -> str:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(text("SELECT association_status FROM auditcore.evidence WHERE tenant_id=:t "
                              "AND di_document_id=:d"), {"t": journey.tenant_id, "d": document_id}).scalar_one()


def _fields(journey, document_id, lines) -> None:
    add_extracted_field(journey, di_document_id=document_id, field_key="line_items", value=lines,
                        document_type="customer_ledger")


def test_the_restore_job_brings_back_a_page_of_its_own_only(journey) -> None:
    from audit_core.uc03_statement_copy_restore import run_statement_copy_restore

    tally = [{"entry_date": "2026-10-01", "particulars_raw": "Vehicle Sales 28%", "debit_amount": 1000000,
              "running_balance": 1000000}]
    requirement = _requirement(journey, "customer_ledger")
    batch = _batch(journey)
    ndc = _copy_on_file(journey, requirement, batch=batch, page=1, sha="a" * 64)
    _fields(journey, ndc, UTKAL)
    _superseded(journey, ndc)  # hidden by the Tally ledger below
    ledger = _copy_on_file(journey, requirement, batch=batch, page=2, sha="b" * 64)
    _fields(journey, ledger, tally)
    older_read = _copy_on_file(journey, requirement, batch=batch, page=2, sha="c" * 64, group=True)
    _fields(journey, older_read, tally[:0])
    _superseded(journey, older_read)  # an earlier reading of the ledger's own page
    twin = _copy_on_file(journey, requirement, batch=batch, page=3, sha="d" * 64)
    _fields(journey, twin, tally)
    _superseded(journey, twin)  # the same reading on another page: adds nothing

    dry = run_statement_copy_restore(journey.engine, f"{journey.tenant_id}:check")
    assert dry["commit"] is False and dry["copies"] == 1 and dry["byKind"] == {"customer_ledger:FORM": 1}
    assert [i["documentId"] for i in dry["items"]] == [str(ndc)]
    assert _status(journey, ndc) == "SUPERSEDED"  # a dry run changes nothing

    done = run_statement_copy_restore(journey.engine, f"{journey.tenant_id}:fix-commit")
    assert done["commit"] is True and done["failed"] == []
    assert (_status(journey, ndc), _status(journey, older_read), _status(journey, twin)) == (
        "ACTIVE", "SUPERSEDED", "SUPERSEDED")
    # idempotent
    assert run_statement_copy_restore(journey.engine, f"{journey.tenant_id}:again")["copies"] == 0


def test_the_restore_job_is_a_one_shot_kind() -> None:
    from audit_core.uc03_p2_worker import _one_shot_target
    from audit_core.uc03_statement_copy_restore import run_statement_copy_restore

    target, args = _one_shot_target("STATEMENT_COPY_RESTORE", None, "t:x", [])
    assert target is run_statement_copy_restore and args == (None, "t:x", [])
