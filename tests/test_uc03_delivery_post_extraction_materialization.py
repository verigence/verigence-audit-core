from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.uc03_delivery_post_extraction_materialization import (
    materialize_delivery_documents_from_durable_store,
)


@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for delivery-materialization integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-dpm-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DPM-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DPM-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'DPM', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"DPM-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DPM-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DPM-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DPM-J-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'DELIVERY', 'DELIVERY_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
        # A Delivery-stage payment requires a real Delivery row linked to the
        # journey (prepare_payment_stage_link()).
        c.execute(
            text(
                "INSERT INTO auditcore.deliveries (tenant_id, journey_id) VALUES (:t, :j)"
            ),
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


def _seed_field(
    conn, *, tenant_id, journey_id, document_id, document_type_key, field_key, value, fact_version=1
):
    conn.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_extracted_fields (
                tenant_id, journey_id, evidence_id, di_document_id,
                source_fact_ref, source_fact_version, stage_code,
                source_document_type_key, source_canonical_field_id, field_key,
                extracted_value, effective_value, is_modified
            ) VALUES (
                :tenant_id, :journey_id, NULL, :document_id,
                NULL, :fact_version, 'DELIVERY',
                :document_type_key, NULL, :field_key,
                CAST(:value AS jsonb), CAST(:value AS jsonb), false
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
            "fact_version": fact_version,
            "document_type_key": document_type_key,
            "field_key": field_key,
            "value": json.dumps(value),
        },
    )


def test_delivery_receipt_registered_as_payment_receipt_materializes_a_payment(journey) -> None:
    # Delivery's own default requirement (0017/0022) registers its receipt
    # document type as "payment_receipt", not Booking's "dealer_receipt" --
    # this is exactly the mismatch fixed in uc03_delivery_review_materialization
    # (_RECEIPT_DOCUMENT_TYPES). A document classified this way must still
    # materialize into auditcore.payments.
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    document_id = uuid4()
    _seed_field(
        journey, tenant_id=tenant_id, journey_id=journey_id, document_id=document_id,
        document_type_key="payment_receipt", field_key="amount_paid", value="50000",
    )
    _seed_field(
        journey, tenant_id=tenant_id, journey_id=journey_id, document_id=document_id,
        document_type_key="payment_receipt", field_key="receipt_number", value="RCPT-001",
    )
    _seed_field(
        journey, tenant_id=tenant_id, journey_id=journey_id, document_id=document_id,
        document_type_key="payment_receipt", field_key="payment_mode", value="NEFT",
    )

    result = materialize_delivery_documents_from_durable_store(
        journey, tenant_id=tenant_id, journey_id=journey_id
    )
    assert not result.get("error"), result

    payment = journey.execute(
        text(
            """
            SELECT amount, payment_method_code
            FROM auditcore.payments
            WHERE tenant_id=:t AND journey_id=:j AND source_di_document_id=:d
            """
        ),
        {"t": tenant_id, "j": journey_id, "d": document_id},
    ).mappings().one_or_none()
    assert payment is not None
    assert float(payment["amount"]) == 50000.0


def test_no_durable_documents_is_a_clean_skip(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    result = materialize_delivery_documents_from_durable_store(
        journey, tenant_id=tenant_id, journey_id=journey_id
    )
    assert result == {"skipped": True, "reason": "no_documents"}
