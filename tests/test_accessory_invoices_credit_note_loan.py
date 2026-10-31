"""Accessory invoices, credit notes and the loan amount without a receipt
(product owner, 2026-10-31).

1. There is no separate Tally accessory invoice: one "Accessory Invoice"
   requirement, met by either format.
2. A deal can carry several accessory invoices: every one is kept, shown and
   added up on the Deal and the Audit Summary -- also when the account
   statement is the Project's source of truth.
3. A credit note (the scrappage case) is shown on the Deal and the Audit
   Summary and never added into the Deal.
4. A loan with no dealership receipt is shown from the Bank Approval Letter,
   marked with an asterisk.
"""
from __future__ import annotations

from decimal import Decimal as D
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text
from test_uc03_account_statement import UTKAL, _ledger, _set_policy, _sync

from audit_core import uc03_p2_audit_summary as summary
from audit_core.db import set_tenant_context
from audit_core.uc03_accessory_invoice_repair import task_plan, to_restore
from audit_core.uc03_deal_source_policy import POLICY_ACCOUNT_STATEMENT
from audit_core.uc03_invoice_materialization import read_invoice, select_invoices
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_stage import requirement_items


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="accn")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _deal(journey) -> dict:
    from audit_core.uc03_p2_journey360 import deal

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return deal(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _row(view: dict, key: str) -> dict:
    return next(r for g in view["categories"] for r in g["components"] if r["key"] == key)


# ── 1. one requirement ───────────────────────────────────────────────────────
def test_the_two_accessory_invoice_formats_are_one_requirement(journey) -> None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        items = requirement_items(c, get_registry(), tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                  stage="DELIVERY", reasons={"accessoriesSold": "The NDC shows accessories."})
    accessory = [i for i in items if set(i["templates"]) & {"accessory_invoice_dms", "accessory_invoice_tally"}]
    assert len(accessory) == 1
    assert accessory[0]["key"] == "ACCESSORY_INVOICE" and accessory[0]["label"] == "Accessory Invoice"
    assert accessory[0]["required"] is True


# ── 2. every accessory invoice counts ────────────────────────────────────────
def test_the_statement_deal_adds_every_accessory_invoice_and_keeps_the_statement_beside_them(journey) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _ledger(journey, UTKAL)  # prints Accessories 30,000
    _sync(journey)
    add_ready_document(journey, "accessory_invoice_dms", process_area="DELIVERY", invoice_number="ACC-1",
                       invoice_date="2026-10-05", grand_total_amount="18000")
    add_ready_document(journey, "accessory_invoice_tally", process_area="DELIVERY", invoice_number="ACC-2",
                       invoice_date="2026-10-05", grand_total_amount="12000")
    # the same bill uploaded twice counts once
    add_ready_document(journey, "accessory_invoice_dms", process_area="DELIVERY", invoice_number="ACC-1",
                       invoice_date="2026-10-05", grand_total_amount="18000")
    view = _deal(journey)
    row = _row(view, "accessories_cost")
    assert D(row["billed"]) == D(30000) and row["billedFrom"] == "invoice"
    assert "INVOICES_DISAGREE" not in row["flags"]  # the statement agrees with the two bills
    shown = [i for i in view["invoices"] if i["documentType"].startswith("accessory_invoice")]
    assert len(shown) == 3 and sum(1 for i in shown if i.get("counted")) == 2
    # the vehicle still comes from the statement
    assert D(_row(view, "ex_showroom_price")["billed"]) == D(1039000)

    out = summary.conclude_charge(row, on_file={"accessory_invoice_dms", "accessory_invoice_tally"})
    assert out["conclusion"] != summary.DISPUTED


def test_two_accessory_bills_add_up_and_are_not_disputed() -> None:
    row = {"key": "accessories_cost", "label": "Accessories", "standard": None, "billed": "12203.02",
           "sources": [{"documentType": "accessory_invoice_dms", "document": "Accessory Invoice (DMS)",
                        "amount": "7457.01", "invoiceNumber": "ACC-1"},
                       {"documentType": "accessory_invoice_tally", "document": "Accessory Invoice (Tally)",
                        "amount": "4746.01", "invoiceNumber": "ACC-2"}]}
    assert summary.disagreement(row) == []
    billed, source = summary.billed_figure(row)
    assert billed == D("12203.02") and source == "2 invoices (ACC-1, ACC-2)"
    # against a statement that prints another figure, it is disputed
    disputed = {**row, "sources": [*row["sources"], {"documentType": "customer_ledger", "document": "NDC",
                                                     "amount": "15000"}]}
    assert {d["amount"] for d in summary.disagreement(disputed)} == {"15000.00", "12203.02"}


def test_superseded_accessory_invoices_that_are_other_bills_are_restored() -> None:
    j = uuid4()

    def doc(status, number, kind="accessory_invoice_dms"):
        return {"journey_id": j, "evidence_id": uuid4(), "di_document_id": uuid4(), "document_type_key": kind,
                "association_status": status, "invoice_number": number}

    active = doc("ACTIVE", "C1R27F000000053", "accessory_invoice_tally")
    other = doc("SUPERSEDED", "C1R27F00000002")
    reupload = doc("SUPERSEDED", "CIR27F000000053")  # the same bill, read with I for 1
    unread = doc("SUPERSEDED", None)
    twice = doc("SUPERSEDED", "C1R27F00000002")
    note = doc("SUPERSEDED", "CN-9", "credit_note")
    assert to_restore([active, other, reupload, unread, twice, note]) == [other, note]


def test_the_dms_task_becomes_the_one_accessory_task() -> None:
    dms = {"task_id": uuid4(), "dedupe_key": "document-missing:j:accessory_invoice_dms"}
    tally = {"task_id": uuid4(), "dedupe_key": "document-missing:j:accessory_invoice_tally"}
    assert task_plan([tally, dms], group_key_taken=False) == [(dms, "MOVE"), (tally, "CANCEL")]
    assert task_plan([tally], group_key_taken=False) == [(tally, "MOVE")]
    assert task_plan([dms, tally], group_key_taken=True) == [(dms, "CANCEL"), (tally, "CANCEL")]


# ── 3. credit notes are shown, never added ───────────────────────────────────
def test_a_credit_note_is_never_added_into_the_deal() -> None:
    vehicle = read_invoice("customer_invoice_dms", {"invoice_number": "INV-1", "grand_total_amount": "500000",
                                                   "vin_number": "MA1VIN0001"}, document_id="v")
    note = read_invoice("credit_note", {"invoice_number": "CN-1", "grand_total_amount": "25000",
                                        "narration": "Scrappage benefit"}, document_id="cn")
    counted = select_invoices([vehicle, note])
    shown = select_invoices([vehicle, note], count_credit_notes=False)
    assert any(g["family"] == "CREDIT_NOTE" for g in counted["counted"])
    assert all(g["family"] != "CREDIT_NOTE" for g in shown["counted"])
    assert shown["netPayable"] == D(500000)


def test_the_statement_deal_lists_the_credit_note(journey) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _ledger(journey, UTKAL)
    _sync(journey)
    before = _deal(journey)["summary"]["net"]["current"]
    add_ready_document(journey, "credit_note", process_area="DELIVERY", invoice_number="CN-7",
                       invoice_date="2026-10-20", grand_total_amount="25000", narration="Scrappage incentive")
    view = _deal(journey)
    assert [(c["number"], D(c["amount"]), c["reason"]) for c in view["creditNotes"]] == \
        [("CN-7", D(25000), "Scrappage incentive")]
    assert view["summary"]["net"]["current"] == before  # information only


# ── 4. the loan without a receipt ────────────────────────────────────────────
def _letter(journey, amount: str = "650000") -> None:
    add_ready_document(journey, "bank_approval_letter", process_area="DELIVERY",
                       finance_sanctioned_amount=amount, financier_name="HDFC BANK LTD",
                       finance_sanction_date="2026-10-10")


def _loan(journey) -> dict | None:
    from audit_core.uc03_p2_journey360 import payments

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return payments(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)["loanWithoutReceipt"]


def test_the_bank_letter_stands_in_for_a_missing_loan_receipt(journey) -> None:
    add_receipt_payment(journey, amount="50000", receipt_number="R-1", receipt_date="2026-10-01")
    _letter(journey)
    loan = _loan(journey)
    assert loan is not None and D(loan["amount"]) == D(650000) and loan["financier"] == "HDFC BANK LTD"
    out = summary.loan_received(matched=None, disbursement_known=False, financed=True, stated=None,
                                receipt_amounts=[D(50000)], letter=loan)
    assert out["amount"] == D(650000) and out["withoutReceipt"] is True
    assert out["source"] == "Bank Approval Letter (sanctioned amount)"


def test_no_asterisk_once_a_receipt_stands_for_the_loan(journey) -> None:
    add_receipt_payment(journey, amount="650000", receipt_number="R-9", receipt_date="2026-10-12")
    _letter(journey)
    assert _loan(journey) is None
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        payment_id = c.execute(text("SELECT payment_id FROM auditcore.payments WHERE tenant_id=:t AND journey_id=:j"),
                               {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one()
    assert payment_id is not None


def test_the_statement_loan_still_wins_over_the_letter() -> None:
    stated = {"loan": D(640000), "refunds": D(0), "source": "NDC (account statement)", "documentId": "d"}
    out = summary.loan_received(matched=None, disbursement_known=False, financed=True, stated=stated,
                                receipt_amounts=[], letter={"amount": "650000.00"})
    assert out["amount"] == D(640000) and out["withoutReceipt"] is False
