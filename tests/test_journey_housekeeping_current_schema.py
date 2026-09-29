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

        # Regression coverage for migration 0071: these six tables (0048, 0065,
        # 0066) were added after the hard-delete function was last updated
        # (0045) and were never wired into it -- a live Super Admin purge hit
        # this exact gap (payment_bank_matches still referencing payments).
        connection.execute(
            text(
                """
                INSERT INTO auditcore.booking_form_review_values (
                    tenant_id, journey_id, source_di_document_id, reviewed_by_actor_id
                ) VALUES (:tenant_id, :journey_id, :di_document_id, 'test-actor')
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "di_document_id": uuid4()},
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.customer_identity_review_values (
                    tenant_id, journey_id, customer_id, source_di_document_id,
                    document_type_key, reviewed_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, :customer_id, :di_document_id,
                    'PAN', 'test-actor'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "customer_id": customer_id,
                "di_document_id": uuid4(),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_receipt_review_values (
                    tenant_id, journey_id, source_di_document_id, reviewed_by_actor_id
                ) VALUES (:tenant_id, :journey_id, :di_document_id, 'test-actor')
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "di_document_id": uuid4()},
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.invoice_review_values (
                    tenant_id, journey_id, source_di_document_id,
                    document_type_key, reviewed_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, :di_document_id,
                    'customer_invoice_dms', 'test-actor'
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "di_document_id": uuid4()},
        )
        # Regression coverage for migration 0093: added in 0079, after this
        # function was last updated in 0071, and never wired in -- a live
        # Super Admin purge hit this exact gap (evidence still referenced by
        # a Scrappage Certificate review-value row).
        connection.execute(
            text(
                """
                INSERT INTO auditcore.scrappage_certificate_review_values (
                    tenant_id, journey_id, source_di_document_id, source_evidence_id,
                    document_type_key, reviewed_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, :di_document_id, :evidence_id,
                    'scrappage_certificate_of_deposit', 'test-actor'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "di_document_id": uuid4(),
                "evidence_id": evidence_id,
            },
        )
        bank_statement_line_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.bank_statement_lines (
                    tenant_id, journey_id, source_di_document_id, reviewed_by_actor_id
                ) VALUES (:tenant_id, :journey_id, :di_document_id, 'test-actor')
                RETURNING bank_statement_line_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "di_document_id": uuid4()},
        ).scalar_one()
        payment_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.payments (
                    tenant_id, journey_id, amount, payment_method_code, payment_reference,
                    receipt_number, receipt_date, payment_stage, status_source
                ) VALUES (
                    :tenant_id, :journey_id, 100000, 'UPI', :ref,
                    'RC-HK', CURRENT_DATE, 'BOOKING', 'EVIDENCE'
                ) RETURNING payment_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "ref": f"HK-PAY-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.payment_bank_matches (
                    tenant_id, journey_id, payment_id, bank_statement_line_id, match_status
                ) VALUES (:tenant_id, :journey_id, :payment_id, :line_id, 'MATCHED')
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "payment_id": payment_id,
                "line_id": bank_statement_line_id,
            },
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
            "payment_bank_matches",
            "bank_statement_lines",
            "invoice_review_values",
            "scrappage_certificate_review_values",
            "booking_form_review_values",
            "customer_identity_review_values",
            "dealer_receipt_review_values",
            "payments",
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


def test_hard_delete_removes_phase2_children_that_block_it() -> None:
    """Regression coverage for migration 0133: four tables added after 0107
    hold a foreign key with no cascade to a table the purge removes, so a
    Journey carrying any of them could not be purged (a 409 to the caller)."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for Journey housekeeping integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-housekeeping-p2-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:code, 'Vehicle') RETURNING product_category_id"),
            {"code": f"HK2-PCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) "
                 "VALUES (:code, 'Housekeeping OEM') RETURNING oem_id"),
            {"code": f"HK2-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id, "
                 "product_category_id, effective_start_date) "
                 "VALUES (:tenant_id, :code, 'Housekeeping Project', :oem_id, :category_id, CURRENT_DATE)"),
            {"tenant_id": tenant_id, "code": f"HK2-P-{suffix}", "oem_id": oem_id, "category_id": category_id},
        )
        dealer_id = connection.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:tenant_id, :code, 'Housekeeping Dealer') RETURNING dealer_id"),
            {"tenant_id": tenant_id, "code": f"HK2-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:tenant_id, :dealer_id, :code, 'Housekeeping Outlet') RETURNING outlet_id"),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"HK2-O-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text("INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, customer_type_code, display_name) "
                 "VALUES (:tenant_id, :dealer_id, :outlet_id, 'PENDING', 'Housekeeping Customer') "
                 "RETURNING customer_id"),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference) "
                 "VALUES (:tenant_id, :dealer_id, :outlet_id, :customer_id, :reference) RETURNING journey_id"),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id,
             "customer_id": customer_id, "reference": f"HK2-{suffix}"},
        ).scalar_one()
        scope = {"tenant_id": tenant_id, "journey_id": journey_id}

        evidence_id = connection.execute(
            text("INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id, "
                 "di_document_id, evidence_purpose) "
                 "VALUES (:tenant_id, :journey_id, :customer_id, :subject, :document, 'BOOKING') "
                 "RETURNING evidence_id"),
            {**scope, "customer_id": customer_id, "subject": uuid4(), "document": uuid4()},
        ).scalar_one()
        # p2_upload_batches.replaces_evidence_id -> evidence
        connection.execute(
            text("INSERT INTO auditcore.p2_upload_batches (tenant_id, journey_id, original_filename, "
                 "size_bytes, original_object_key, uploaded_by_actor_id, replaces_evidence_id) "
                 "VALUES (:tenant_id, :journey_id, 'scan.pdf', 10, :key, 'test-actor', :evidence_id)"),
            {**scope, "key": f"p2-documents/{suffix}", "evidence_id": evidence_id},
        )
        # rule_executions.audit_finding_id -> audit_findings
        finding_id = connection.execute(
            text("INSERT INTO auditcore.audit_findings (tenant_id, journey_id, title) "
                 "VALUES (:tenant_id, :journey_id, 'Finding') RETURNING audit_finding_id"),
            scope,
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.rule_executions (tenant_id, journey_id, rule_code, "
                 "triggering_event, outcome, audit_finding_id) "
                 "VALUES (:tenant_id, :journey_id, 'RULE', 'EVENT', 'FAIL', :finding_id)"),
            {**scope, "finding_id": finding_id},
        )
        # delivery_vehicle_photos -> journeys
        connection.execute(
            text("INSERT INTO auditcore.delivery_vehicle_photos (tenant_id, journey_id, object_key, "
                 "original_filename, content_type, size_bytes, uploaded_by_actor_id) "
                 "VALUES (:tenant_id, :journey_id, :key, 'car.jpg', 'image/jpeg', 10, 'test-actor')"),
            {**scope, "key": f"p2-photos/{suffix}"},
        )
        # journey_delivery_vin_observation_proposals -> journeys and workflow_tasks
        instance_id = connection.execute(
            text("INSERT INTO auditcore.workflow_instances (tenant_id, workflow_type, journey_id) "
                 "VALUES (:tenant_id, 'DELIVERY', :journey_id) RETURNING workflow_instance_id"),
            scope,
        ).scalar_one()
        task_id = connection.execute(
            text("INSERT INTO auditcore.workflow_tasks (tenant_id, workflow_instance_id, process_area, "
                 "task_type, journey_id) "
                 "VALUES (:tenant_id, :instance_id, 'DELIVERY', 'VIN', :journey_id) RETURNING workflow_task_id"),
            {**scope, "instance_id": instance_id},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.journey_delivery_vin_observation_proposals (tenant_id, "
                 "workflow_task_id, journey_id, computed_reconciliation_status, proposed_by_actor_id) "
                 "VALUES (:tenant_id, :task_id, :journey_id, 'MATCH', 'test-actor')"),
            {**scope, "task_id": task_id},
        )

        receipt = connection.execute(
            text("SELECT auditcore.hard_delete_journey_transactions(:tenant_id, CAST(:journey_ids AS uuid[]))"),
            {"tenant_id": tenant_id, "journey_ids": [journey_id]},
        ).scalar_one()
        assert receipt is not None

        for table in (
            "p2_upload_batches",
            "rule_executions",
            "delivery_vehicle_photos",
            "journey_delivery_vin_observation_proposals",
            "audit_findings",
            "workflow_tasks",
            "evidence",
            "journeys",
        ):
            remaining = connection.execute(
                text(f"SELECT count(*) FROM auditcore.{table} WHERE tenant_id=:tenant_id"),
                {"tenant_id": tenant_id},
            ).scalar_one()
            assert remaining == 0, table
