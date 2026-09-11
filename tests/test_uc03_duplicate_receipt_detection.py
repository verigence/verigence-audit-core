from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_duplicate_receipt_detection as drd


@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for duplicate-receipt integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-drd-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DRD-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DRD-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'DRD', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"DRD-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DRD-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DRD-O-{suffix}"},
        ).scalar_one()
        customer_id = c.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DRD-J-{suffix}"},
        ).scalar_one()
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _set_receipt_field(c, *, stage_code, document_type_key, field_key, value, document_id):
    c.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_extracted_fields (
                tenant_id, journey_id, evidence_id, di_document_id,
                source_fact_ref, source_fact_version, stage_code,
                source_document_type_key, source_canonical_field_id, field_key,
                extracted_value, effective_value, is_modified
            ) VALUES (
                :t, :j, NULL, :doc,
                NULL, 1, :stage,
                :dtk, NULL, :fk,
                CAST(:v AS jsonb), CAST(:v AS jsonb), false
            )
            """
        ),
        {"t": c.tenant_id, "j": c.journey_id, "doc": document_id, "stage": stage_code,
         "dtk": document_type_key, "fk": field_key, "v": json.dumps(value)},
    )


def _seed_receipt(c, *, stage_code, document_type_key, receipt_number=None, amount, receipt_date, document_id=None):
    document_id = document_id or uuid4()
    if receipt_number is not None:
        _set_receipt_field(c, stage_code=stage_code, document_type_key=document_type_key,
                            field_key="receipt_number", value=receipt_number, document_id=document_id)
    _set_receipt_field(c, stage_code=stage_code, document_type_key=document_type_key,
                        field_key="amount_paid", value=amount, document_id=document_id)
    _set_receipt_field(c, stage_code=stage_code, document_type_key=document_type_key,
                        field_key="receipt_date", value=receipt_date, document_id=document_id)
    return document_id


def _open_duplicate_findings(c) -> list[dict]:
    return [
        dict(row)
        for row in c.execute(
            text("SELECT rule_key, severity, finding_status FROM auditcore.audit_findings "
                 "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='DUPLICATE_RECEIPT' "
                 "AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
            {"t": c.tenant_id, "j": c.journey_id},
        ).mappings().all()
    ]


def test_same_receipt_uploaded_five_times_is_one_duplicate_group(journey) -> None:
    c = journey
    for _ in range(5):
        _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                      receipt_number="RC-1001", amount="200000", receipt_date="2026-08-01")

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["groupCount"] == 1
    findings = _open_duplicate_findings(c)
    assert len(findings) == 1
    assert findings[0]["severity"] == "HIGH"

    classified = c.execute(
        text("SELECT finding_class, owner_role_code FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='DUPLICATE_RECEIPT'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert classified["finding_class"] == "VIOLATION"
    assert classified["owner_role_code"] == "TL"


def test_different_receipt_numbers_same_amount_are_not_flagged(journey) -> None:
    # Two genuinely separate real payments of the same round amount, each
    # with its own distinct receipt number, must not be flagged.
    c = journey
    _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                  receipt_number="RC-2001", amount="100000", receipt_date="2026-08-01")
    _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                  receipt_number="RC-2002", amount="100000", receipt_date="2026-08-05")

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["groupCount"] == 0
    assert _open_duplicate_findings(c) == []


def test_dealer_receipt_and_payment_receipt_same_amount_are_not_cross_flagged(journey) -> None:
    # A Booking advance (dealer_receipt) and a Delivery balance (payment_receipt)
    # for the same amount are two different real payments -- same type only.
    c = journey
    _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                  receipt_number="RC-3001", amount="100000", receipt_date="2026-08-01")
    _seed_receipt(c, stage_code="DELIVERY", document_type_key="payment_receipt",
                  receipt_number="RC-3001", amount="100000", receipt_date="2026-08-01")

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["groupCount"] == 0
    assert _open_duplicate_findings(c) == []


def test_no_receipt_number_falls_back_to_amount_and_date(journey) -> None:
    c = journey
    _seed_receipt(c, stage_code="DELIVERY", document_type_key="payment_receipt",
                  amount="150000", receipt_date="2026-08-10")
    _seed_receipt(c, stage_code="DELIVERY", document_type_key="payment_receipt",
                  amount="150000", receipt_date="2026-08-10")

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["groupCount"] == 1
    findings = _open_duplicate_findings(c)
    assert len(findings) == 1


def test_no_receipt_number_different_dates_are_not_flagged(journey) -> None:
    c = journey
    _seed_receipt(c, stage_code="DELIVERY", document_type_key="payment_receipt",
                  amount="150000", receipt_date="2026-08-10")
    _seed_receipt(c, stage_code="DELIVERY", document_type_key="payment_receipt",
                  amount="150000", receipt_date="2026-08-15")

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["groupCount"] == 0
    assert _open_duplicate_findings(c) == []


def test_correction_that_breaks_the_match_self_heals(journey) -> None:
    c = journey
    doc_a = _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                          receipt_number="RC-4001", amount="200000", receipt_date="2026-08-01")
    _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                  receipt_number="RC-4001", amount="200000", receipt_date="2026-08-01")

    drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert len(_open_duplicate_findings(c)) == 1

    # A PC correction reveals these were actually two different receipt numbers.
    c.execute(
        text("UPDATE auditcore.journey_document_extracted_fields "
             "SET effective_value = CAST(:v AS jsonb) "
             "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:doc AND field_key='receipt_number'"),
        {"v": json.dumps("RC-4001-A"), "t": c.tenant_id, "j": c.journey_id, "doc": doc_a},
    )

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["resolved"] == 1
    assert _open_duplicate_findings(c) == []
