"""BOOKING_REBUILD: a booking whose recompute would change something is
recomputed; the job never writes a booking's values itself (2026-11-01)."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_extracted_field,
    add_ready_document,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_booking_rebuild as rebuild
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_worker import _one_shot_target, settle_journey


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2rb")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _receipt(journey, *, amount: str | None, number: str):
    document_id = add_ready_document(journey, "dealer_receipt", receipt_number=number, receipt_date="2026-09-25",
                                     customer_name="MATRUKRUPA ENTERPRISERS")
    add_extracted_field(journey, di_document_id=document_id, field_key="amount_paid", value=amount, confidence=99.0,
                        document_type="dealer_receipt")
    return str(document_id)


def _payments(journey) -> list[dict]:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return [dict(r) for r in c.execute(
            text("SELECT amount, receipt_number FROM auditcore.payments WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id}).mappings().all()]


def _version(journey) -> int:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(text("SELECT COALESCE(MAX(fact_version), 0) FROM auditcore.p2_journey_runtime "
                              "WHERE tenant_id=:t AND journey_id=:j"),
                         {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one()


def _check(journey) -> dict:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return rebuild.rebuild_check(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _settle(journey) -> None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        settle_journey(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def test_the_job_is_registered(journey) -> None:
    target, args = _one_shot_target("BOOKING_REBUILD", journey.engine, "t", [])
    assert target.__name__ == "run_booking_rebuild" and journey.tenant_id in args[2]


def test_a_receipt_with_no_payment_is_found_and_nothing_is_written(journey) -> None:
    """UTK-SB-261028-001: Rs 30,36,490 read, no payment, booking unpaid."""
    document_id = _receipt(journey, amount="3036490", number="BRMM/2627/08128")
    changes = _check(journey)
    added = changes["payments"]["addedPayments"]
    assert [(float(p["amount"]), p["receiptNumber"], p["documentId"]) for p in added] == [
        (3036490.0, "BRMM/2627/08128", document_id)]
    assert _payments(journey) == []  # always rolled back


def test_the_dry_run_requests_nothing_and_the_commit_requests_the_recompute(journey) -> None:
    _receipt(journey, amount="1100000", number="R-1")
    version = _version(journey)
    totals = rebuild.rebuild_tenant(journey.engine, tenant_id=journey.tenant_id, tag="t", commit=False)
    assert totals["wouldChange"] == 1 and totals["paymentsAdded"] == 1
    assert totals["paymentsAddedAmount"] == "1100000.00" and totals["recomputeRequested"] == 0
    assert _version(journey) == version
    totals = rebuild.rebuild_tenant(journey.engine, tenant_id=journey.tenant_id, tag="t-commit", commit=True)
    assert totals["recomputeRequested"] == 1 and totals["failed"] == 0
    assert _version(journey) == version + 1
    assert _payments(journey) == []  # the worker's recompute builds it


def test_a_booking_already_recomputed_is_not_touched(journey) -> None:
    """When and by whom a row was written is not a change: a booking as its
    last recompute left it would change nothing."""
    add_ready_document(journey, "booking_form", model="SCORPIO N", variant="Z8L", color="RED",
                       ex_showroom_price="1500000", customer_name="P2 Customer", booking_date="2026-10-01")
    add_ready_document(journey, "pan_card", pan_number="ABCDE1234F", pan_name="P2 Customer")
    _receipt(journey, amount="21000", number="R-2")
    _settle(journey)
    assert len(_payments(journey)) == 1
    version = _version(journey)
    assert _check(journey) == {}
    totals = rebuild.rebuild_tenant(journey.engine, tenant_id=journey.tenant_id, tag="t-commit", commit=True)
    assert totals["wouldChange"] == 0 and totals["recomputeRequested"] == 0
    assert _version(journey) == version


def test_a_receipt_with_no_amount_read_is_found_for_its_task(journey) -> None:
    """2026-11-01: 48 receipts on file had no amount read and no task; the
    recompute this job requests raises it."""
    _receipt(journey, amount=None, number="R-3")
    changes = _check(journey)
    assert "MANUAL_VERIFICATION: Enter the amount on Booking Payment Receipt: it was not read" in \
        changes["tasks"]["raised"]
    assert "payments" not in changes
    totals = rebuild.rebuild_tenant(journey.engine, tenant_id=journey.tenant_id, tag="t", commit=False)
    assert totals["wouldChange"] == 1 and totals["raisedByCode"].get("MANUAL_VERIFICATION") == 1


def test_compare_lists_payments_and_ignores_write_stamps_and_sku_names() -> None:
    before = {"payments": {"a": {"amount": 100, "receipt_number": "X"}, "b": {"amount": 5, "receipt_number": "Y"}},
              "customers": {"c": {"legal_name": "A", "legal_name_verified_at_utc": "t1", "updated_by_actor_id": "x"}},
              "journey_products": {"p": {"product_sku_id": "sku", "model_name_snapshot": "XUV3XO EV"}}}
    after = {"payments": {"a": {"amount": 150, "receipt_number": "X"}},
             "customers": {"c": {"legal_name": "A", "legal_name_verified_at_utc": "t2", "updated_by_actor_id": "y"}},
             "journey_products": {"p": {"product_sku_id": "sku", "model_name_snapshot": "XUV 3XO"}}}
    changes = rebuild.compare(before, after)
    assert set(changes) == {"payments"}
    payments = changes["payments"]
    assert payments["removed"] == 1 and payments["removedPayments"][0]["receiptNumber"] == "Y"
    assert payments["changedPayments"][0]["amountBefore"] == 100 and payments["changedPayments"][0]["columns"] == [
        "amount"]
    unresolved = {"journey_products": {"p": {"product_sku_id": None, "model_name_snapshot": None}}}
    named = {"journey_products": {"p": {"product_sku_id": None, "model_name_snapshot": "SCORPIO N"}}}
    assert rebuild.compare(unresolved, named)["journey_products"]["changed"] == 1
    assert rebuild.compare(after, after) == {}
