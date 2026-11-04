"""Every document on file gives its key figures, or the booking says it did not
(product owner 2026-11-04: "all these should have been identified"; "All equal,
HIGH"). Real Postgres."""
from __future__ import annotations

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
from audit_core.uc03_document_key_figures import (
    KEY_FIGURES,
    TASK_TYPE,
    documents_not_fully_read,
    missing,
    sync_document_key_figures,
)


# ── the rule table, without a database ───────────────────────────────────────
def _labels(document_type: str, fields: dict) -> list[str]:
    return [f.label for f in missing(document_type, fields)]


def test_a_receipt_needs_its_amount_date_number_and_mode():
    full = {"amount_paid": "21000", "receipt_date": "2026-09-21", "receipt_number": "MR/7867",
            "payment_mode": "UPI"}
    assert _labels("dealer_receipt", full) == []
    assert _labels("dealer_receipt", {**full, "amount_paid": None}) == ["amount"]
    assert _labels("dealer_receipt", {**full, "amount_paid": "0"}) == ["amount"]  # zero is no reading of an amount
    assert _labels("dealer_receipt", {**full, "payment_mode": '""', "receipt_number": "null"}) == [
        "receipt number", "payment mode"]


def test_one_figure_under_either_of_its_names_is_enough():
    invoice = {"grand_total_amount": "2979501", "invoice_number": "INV27C001593", "invoice_date": "2026-10-05",
               "taxable_amount": "2809524.9"}
    assert _labels("customer_invoice_dms", {**invoice, "chassis_number": "MA1VP271BT6J92553"}) == []
    assert _labels("customer_invoice_dms", invoice) == ["VIN"]


def test_every_figure_listed_for_each_type():
    assert {f.label for f in KEY_FIGURES["insurance_cover"]} == {
        "premium", "policy number", "insurer", "policy start date", "chassis number"}
    assert {f.label for f in KEY_FIGURES["booking_docket"]} == {"booking date", "customer name", "mobile", "model"}
    assert _labels("unknown_type", {}) == []


# ── on the database ──────────────────────────────────────────────────────────
@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="keyfig")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _document(journey, document_type: str, fields: dict, *, queue_status: str | None = "READY", page: int = 1):
    document_id = uuid4()
    if queue_status is not None:
        add_page(journey, page_number=page, status=queue_status, di_document_id=document_id)
    add_evidence(journey, di_document_id=document_id, document_type_key=document_type)
    for key, value in fields.items():
        add_extracted_field(journey, di_document_id=document_id, field_key=key, value=value,
                            document_type=document_type)
    return document_id


def _sync(journey) -> dict:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return sync_document_key_figures(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _task(journey) -> dict | None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        row = c.execute(
            text("SELECT task_status, severity, title, description, assigned_role_code FROM auditcore.p2_tasks "
                 "WHERE tenant_id=:t AND journey_id=:j AND task_type=:k"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": TASK_TYPE}).mappings().one_or_none()
    return dict(row) if row else None


def test_a_figure_not_read_raises_one_high_task_and_closes_when_read(journey):
    receipt = _document(journey, "dealer_receipt", {"receipt_date": "2026-09-21", "receipt_number": "MR/7867",
                                                    "payment_mode": "UPI"}, page=3)
    _document(journey, "insurance_cover", {"premium_amount": "170879", "policy_number": "P-1",
                                           "insurer_name": "ICICI", "policy_start_date": "2026-10-05"}, page=7)
    assert _sync(journey) == {"examined": 2, "documents": 2}
    task = _task(journey)
    assert task["severity"] == "HIGH" and task["assigned_role_code"] == "PC"
    assert task["title"] == "Figures not read from documents on file"
    assert "Dealer receipt (page 3): amount" in task["description"]
    assert "Insurance policy (page 7): chassis number" in task["description"]
    # Corrected by a person (or read again): the task closes by itself.
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("DELETE FROM auditcore.evidence WHERE tenant_id=:t AND journey_id=:j "
                       "AND document_type_key='insurance_cover'"), {"t": journey.tenant_id, "j": journey.journey_id})
    add_extracted_field(journey, di_document_id=receipt, field_key="amount_paid", value="21000",
                        document_type="dealer_receipt")
    assert _sync(journey) == {"examined": 1, "documents": 0}
    assert _task(journey)["task_status"] == "VERIFIED_COMPLETE"


def test_a_page_still_being_read_is_not_judged_yet(journey):
    _document(journey, "dealer_receipt", {}, queue_status="EXTRACTING")
    assert _sync(journey) == {"examined": 0, "documents": 0} and _task(journey) is None


def test_a_document_with_no_page_row_is_judged(journey):
    _document(journey, "pan_card", {}, queue_status=None)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        [doc] = documents_not_fully_read(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert doc["document"] == "PAN card" and doc["missing"] == ["PAN"] and doc["pageNumber"] is None


def test_the_pc_can_close_it_when_a_figure_is_not_printed(journey):
    from audit_core.uc03_p2_tasks import CLOSE_TASK_TYPES

    assert TASK_TYPE in CLOSE_TASK_TYPES
