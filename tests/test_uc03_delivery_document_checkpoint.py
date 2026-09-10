from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.uc03_delivery_capture_v2 import schedule_delivery_document_checkpoint


@pytest.fixture
def delivery_journey():
    """Same shape as test_uc03_delivery_conditional_applicability.py's fixture."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-dlchk-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DC-CAT-{suffix[:8]}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DC-OEM-{suffix[:8]}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id, effective_start_date)
                VALUES (:t, :pc, 'DA', :o, :cat, CURRENT_DATE - 60)"""),
            {"t": tenant_id, "pc": f"DC-{suffix[:8]}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DC-D-{suffix[:8]}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DC-O-{suffix[:8]}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DC-J-{suffix[:8]}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'DELIVERY', 'DELIVERY_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _required_requirement(c, *, requirement_key: str, document_type_key: str | None = None):
    return c.execute(
        text("""
            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, requirement_key, document_type_key,
                process_area, requirement_level
            ) VALUES (
                :t, :j, :key, :doc_type, 'DELIVERY', 'REQUIRED'
            ) RETURNING journey_document_requirement_id
        """),
        {
            "t": c.tenant_id, "j": c.journey_id, "key": requirement_key,
            "doc_type": document_type_key or requirement_key,
        },
    ).scalar_one()


def _capture_document(c, *, di_document_id, requirement_key: str, capture_status: str):
    c.execute(
        text("""
            INSERT INTO auditcore.document_capture_v2_documents (
                tenant_id, journey_id, stage_code, di_document_id, client_upload_id,
                requirement_key, classified_document_type_key, capture_status, created_by_actor_id
            ) VALUES (
                :t, :j, 'DELIVERY', :doc_id, :upload_id, :req_key, :req_key, :status, 'test-actor'
            )
        """),
        {
            "t": c.tenant_id, "j": c.journey_id, "doc_id": di_document_id,
            "upload_id": f"upload-{di_document_id}", "req_key": requirement_key, "status": capture_status,
        },
    )


def _set_capture_status(c, *, di_document_id, capture_status: str):
    c.execute(
        text("""
            UPDATE auditcore.document_capture_v2_documents
            SET capture_status=:status
            WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:doc_id
        """),
        {"t": c.tenant_id, "j": c.journey_id, "doc_id": di_document_id, "status": capture_status},
    )


def _finding_status(c, *, rule_key: str) -> str | None:
    return c.execute(
        text("""
            SELECT finding_status FROM auditcore.audit_findings
            WHERE tenant_id=:t AND journey_id=:j AND rule_key=:rule_key
        """),
        {"t": c.tenant_id, "j": c.journey_id, "rule_key": rule_key},
    ).scalar_one_or_none()


def test_missing_document_finding_self_heals_once_the_document_lands(delivery_journey) -> None:
    """Regression: DL_V2_REQUIRED_DOCUMENT_MISSING was only ever evaluated once,
    at Submit -- uploading the missing document afterward never cleared it."""
    c = delivery_journey
    _required_requirement(c, requirement_key="accessory_invoice_dms")

    raised, resolved = schedule_delivery_document_checkpoint(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="test",
    )
    assert raised
    assert not resolved
    assert _finding_status(c, rule_key="DL_V2_REQUIRED_DOCUMENT_MISSING:accessory_invoice_dms") == "OPEN"

    _capture_document(
        c, di_document_id=uuid4(), requirement_key="accessory_invoice_dms", capture_status="CLASSIFIED",
    )

    raised, resolved = schedule_delivery_document_checkpoint(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="test",
    )
    assert not raised
    assert resolved
    assert _finding_status(c, rule_key="DL_V2_REQUIRED_DOCUMENT_MISSING:accessory_invoice_dms") == "RESOLVED"


def test_processing_failed_finding_self_heals_once_the_document_recovers(delivery_journey) -> None:
    c = delivery_journey
    document_id = uuid4()
    _capture_document(c, di_document_id=document_id, requirement_key="rto_challan", capture_status="FAILED")

    raised, _resolved = schedule_delivery_document_checkpoint(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="test",
    )
    assert raised
    assert _finding_status(c, rule_key=f"DL_V2_DOCUMENT_PROCESSING_FAILED:{document_id}") == "OPEN"

    _set_capture_status(c, di_document_id=document_id, capture_status="CLASSIFIED")

    _raised, resolved = schedule_delivery_document_checkpoint(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="test",
    )
    assert resolved
    assert _finding_status(c, rule_key=f"DL_V2_DOCUMENT_PROCESSING_FAILED:{document_id}") == "RESOLVED"


def test_not_applicable_requirement_never_raises_a_missing_document_finding(delivery_journey) -> None:
    c = delivery_journey
    requirement_id = _required_requirement(c, requirement_key="ew_invoice")
    c.execute(
        text("""
            UPDATE auditcore.journey_document_requirements
            SET requirement_status='NOT_APPLICABLE'
            WHERE journey_document_requirement_id=:r
        """),
        {"r": requirement_id},
    )

    raised, resolved = schedule_delivery_document_checkpoint(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="test",
    )
    assert not raised
    assert not resolved
    assert _finding_status(c, rule_key="DL_V2_REQUIRED_DOCUMENT_MISSING:ew_invoice") is None
