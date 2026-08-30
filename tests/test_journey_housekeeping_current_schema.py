from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text


def test_hard_delete_removes_post_0026_uc03_children_before_parents() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for Journey housekeeping integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-housekeeping-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"HK-PCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Housekeeping OEM') RETURNING oem_id"
            ),
            {"code": f"HK-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Housekeeping Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"HK-P-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Housekeeping Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"HK-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Housekeeping Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"HK-O-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'PENDING', 'Housekeeping Customer'
                ) RETURNING customer_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id, journey_reference
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, :journey_reference
                ) RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
                "customer_id": customer_id,
                "journey_reference": f"HK-{suffix}",
            },
        ).scalar_one()

        evidence_di_document_id = uuid4()
        evidence_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id,
                    di_subject_id, di_document_id, evidence_purpose
                ) VALUES (
                    :tenant_id, :journey_id, :customer_id,
                    :di_subject_id, :di_document_id, 'BOOKING'
                ) RETURNING evidence_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "customer_id": customer_id,
                "di_subject_id": uuid4(),
                "di_document_id": evidence_di_document_id,
            },
        ).scalar_one()

        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_extracted_fields (
                    tenant_id, journey_id, evidence_id, di_document_id,
                    source_fact_ref, source_fact_version, field_key, extracted_value
                ) VALUES (
                    :tenant_id, :journey_id, :evidence_id, :di_document_id,
                    :source_fact_ref, 1, 'customer_name', to_jsonb('Doc Name'::text)
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "evidence_id": evidence_id,
                "di_document_id": evidence_di_document_id,
                "source_fact_ref": uuid4(),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_attribute_resolutions (
                    tenant_id, journey_id, stage_code, attribute_key,
                    mapping_status, source_di_document_id, source_evidence_id,
                    source_field_key, source_fact_version, resolution_rule,
                    mapping_version, resolved_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING', 'customer_name',
                    'SUPPORTED', :di_document_id, :evidence_id,
                    'customer_name', 1, 'EXPLICIT_MAPPING',
                    'housekeeping-test', 'test-actor'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "di_document_id": evidence_di_document_id,
                "evidence_id": evidence_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_attribute_review_decisions (
                    tenant_id, journey_id, stage_code, review_key, review_kind,
                    decision, source_set_ref, source_di_document_id,
                    source_field_key, source_fact_version, decided_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING', 'customer_name', 'ATTRIBUTE',
                    'ACCEPTED', 'housekeeping-test-source', :di_document_id,
                    'customer_name', 1, 'test-actor'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "di_document_id": evidence_di_document_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.document_capture_v2_documents (
                    tenant_id, journey_id, stage_code, di_document_id,
                    client_upload_id, capture_status, created_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING', :di_document_id,
                    'housekeeping-upload', 'CLASSIFIED', 'test-actor'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "di_document_id": uuid4(),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.document_capture_v2_declarations (
                    tenant_id, journey_id, stage_code, condition_key,
                    applicable, document_available, declared_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING', 'gstApplicable',
                    false, NULL, 'test-actor'
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        )

        receipt = connection.execute(
            text(
                "SELECT auditcore.hard_delete_journey_transactions(" 
                ":tenant_id, CAST(:journey_ids AS uuid[]))"
            ),
            {"tenant_id": tenant_id, "journey_ids": [journey_id]},
        ).scalar_one()
        assert receipt is not None

        for table in (
            "journey_attribute_review_decisions",
            "journey_attribute_resolutions",
            "journey_document_extracted_fields",
            "document_capture_v2_documents",
            "document_capture_v2_declarations",
            "evidence",
            "journeys",
        ):
            remaining = connection.execute(
                text(
                    f"SELECT count(*) FROM auditcore.{table} "
                    "WHERE tenant_id=:tenant_id"
                ),
                {"tenant_id": tenant_id},
            ).scalar_one()
            assert remaining == 0, table
