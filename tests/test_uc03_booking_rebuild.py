"""BOOKING_REBUILD: a booking whose build would change is recomputed; the job
never writes a booking's values itself (2026-11-01)."""
from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_booking_rebuild as rebuild
from audit_core.db import set_tenant_context


@pytest.fixture
def booking():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for booking rebuild integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-rb-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"), {"c": f"RB-CAT-{suffix}"}).scalar_one()
        oem_id = c.execute(text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
                           {"c": f"RB-OEM-{suffix}"}).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'RB', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"RB-{suffix}", "o": oem_id, "cat": category_id})
        dealer_id = c.execute(text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                                   "VALUES (:t, :c, 'D') RETURNING dealer_id"),
                              {"t": tenant_id, "c": f"RB-D-{suffix}"}).scalar_one()
        outlet_id = c.execute(text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                                   "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
                              {"t": tenant_id, "d": dealer_id, "c": f"RB-O-{suffix}"}).scalar_one()
        customer_id = c.execute(text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
                                {"t": tenant_id, "d": dealer_id, "o": outlet_id}).scalar_one()
        journey_id = c.execute(text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
                               {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id,
                                "r": f"RB-J-{suffix}"}).scalar_one()
    yield engine, tenant_id, journey_id
    engine.dispose()


def _receipt(engine, tenant_id, journey_id, *, amount: str, number: str) -> str:
    document_id = uuid4()
    with engine.begin() as c:
        set_tenant_context(c, tenant_id)
        evidence_id = c.execute(
            text("""INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id,
                        di_document_id, document_type_key, evidence_purpose, association_status)
                    SELECT j.tenant_id, j.journey_id, j.customer_id, :s, :d, 'dealer_receipt', 'BOOKING_DOCUMENT', 'ACTIVE'
                    FROM auditcore.journeys j WHERE j.tenant_id=:t AND j.journey_id=:j
                    RETURNING evidence_id"""),
            {"t": tenant_id, "j": journey_id, "d": document_id, "s": uuid4()}).scalar_one()
        for field_key, value in (("amount_paid", amount), ("receipt_number", number),
                                 ("receipt_date", "2026-09-25"), ("customer_name", "MATRUKRUPA ENTERPRISERS")):
            c.execute(
                text("""INSERT INTO auditcore.journey_document_extracted_fields (
                            tenant_id, journey_id, evidence_id, di_document_id, source_fact_ref,
                            source_fact_version, stage_code, source_document_type_key, source_canonical_field_id,
                            field_key, extracted_value, effective_value, confidence_score, is_modified)
                        VALUES (:t, :j, :ev, :d, NULL, 1, 'BOOKING', 'dealer_receipt', NULL, :fk,
                                CAST(:v AS jsonb), CAST(:v AS jsonb), 0.92, false)"""),
                {"t": tenant_id, "j": journey_id, "ev": evidence_id, "d": document_id, "fk": field_key,
                 "v": json.dumps(value)})
    return str(document_id)


def _payments(engine, tenant_id, journey_id) -> list[dict]:
    with engine.begin() as c:
        set_tenant_context(c, tenant_id)
        return [dict(r) for r in c.execute(
            text("SELECT amount, receipt_number, source_di_document_id FROM auditcore.payments "
                 "WHERE tenant_id=:t AND journey_id=:j"), {"t": tenant_id, "j": journey_id}).mappings().all()]


def _recompute_version(engine, tenant_id, journey_id) -> int:
    with engine.begin() as c:
        set_tenant_context(c, tenant_id)
        return c.execute(text("SELECT COALESCE(MAX(fact_version), 0) FROM auditcore.p2_journey_runtime "
                              "WHERE tenant_id=:t AND journey_id=:j"), {"t": tenant_id, "j": journey_id}).scalar_one()


def test_a_receipt_with_no_payment_is_found_and_nothing_is_written(booking) -> None:
    """UTK-SB-261028-001: ₹30,36,490 read, no payment, booking unpaid."""
    engine, tenant_id, journey_id = booking
    document_id = _receipt(engine, tenant_id, journey_id, amount="3036490", number="BRMM/2627/08128")
    with engine.begin() as c:
        set_tenant_context(c, tenant_id)
        changes = rebuild.rebuild_check(c, tenant_id=tenant_id, journey_id=journey_id)
    added = changes["payments"]["addedPayments"]
    assert [(float(p["amount"]), p["receiptNumber"], p["documentId"]) for p in added] == [
        (3036490.0, "BRMM/2627/08128", document_id)]
    assert _payments(engine, tenant_id, journey_id) == []  # always rolled back


def test_the_dry_run_requests_nothing_and_the_commit_requests_the_recompute(booking) -> None:
    engine, tenant_id, journey_id = booking
    _receipt(engine, tenant_id, journey_id, amount="1100000", number="R-1")
    version = _recompute_version(engine, tenant_id, journey_id)
    totals = rebuild.rebuild_tenant(engine, tenant_id=tenant_id, tag="t", commit=False)
    assert totals["wouldChange"] == 1 and totals["paymentsAdded"] == 1
    assert totals["paymentsAddedAmount"] == "1100000.00" and totals["recomputeRequested"] == 0
    assert _recompute_version(engine, tenant_id, journey_id) == version
    totals = rebuild.rebuild_tenant(engine, tenant_id=tenant_id, tag="t-commit", commit=True)
    assert totals["recomputeRequested"] == 1 and totals["failed"] == 0
    assert _recompute_version(engine, tenant_id, journey_id) == version + 1
    assert _payments(engine, tenant_id, journey_id) == []  # the worker's recompute builds it


def test_a_booking_already_built_is_not_touched(booking) -> None:
    from audit_core.uc03_p2_worker import _materialize_journey

    engine, tenant_id, journey_id = booking
    _receipt(engine, tenant_id, journey_id, amount="21000", number="R-2")
    with engine.begin() as c:
        set_tenant_context(c, tenant_id)
        _materialize_journey(c, tenant_id=tenant_id, journey_id=journey_id)
    assert len(_payments(engine, tenant_id, journey_id)) == 1
    version = _recompute_version(engine, tenant_id, journey_id)
    totals = rebuild.rebuild_tenant(engine, tenant_id=tenant_id, tag="t-commit", commit=True)
    assert totals["wouldChange"] == 0 and totals["recomputeRequested"] == 0
    assert _recompute_version(engine, tenant_id, journey_id) == version


def test_compare_lists_removed_and_changed_payments() -> None:
    before = {"payments": {"a": {"amount": 100, "receipt_number": "X"}, "b": {"amount": 5, "receipt_number": "Y"}}}
    after = {"payments": {"a": {"amount": 150, "receipt_number": "X"}}}
    changes = rebuild.compare(before, after)["payments"]
    assert changes["removed"] == 1 and changes["removedPayments"][0]["receiptNumber"] == "Y"
    assert changes["changedPayments"][0]["amountBefore"] == 100 and changes["changedPayments"][0]["columns"] == ["amount"]
    assert rebuild.compare(after, after) == {}
