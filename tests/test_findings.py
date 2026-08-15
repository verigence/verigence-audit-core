from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal


def test_finding_links_evidence_fact_without_mutating_delivery_status() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for findings integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-finding-{suffix}"
    actor_id = f"tl-{suffix}"
    di_subject_id = uuid4()
    di_document_id = uuid4()

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"FCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Finding OEM') RETURNING oem_id"
            ),
            {"code": f"FOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Finding Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"FP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_status_codes (
                    tenant_id, domain_key, status_code, status_label
                ) VALUES (:tenant_id, 'DELIVERY', 'DELIVERED_TEST', 'Delivered')
                """
            ),
            {"tenant_id": tenant_id},
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Finding Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"FD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Finding Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"FO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Finding Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'FINDING-JOURNEY'
                ) RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
                "customer_id": customer_id,
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code,
                    dealer_id, outlet_id
                ) VALUES (
                    :tenant_id, :actor_id, 'TL', :dealer_id, :outlet_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
            },
        )
        evidence_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id,
                    di_subject_id, di_document_id,
                    document_type_key, evidence_purpose, linked_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, :customer_id,
                    :di_subject_id, :di_document_id,
                    'DELIVERY_RECEIPT', 'DELIVERY_AUDIT', :actor_id
                ) RETURNING evidence_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "customer_id": customer_id,
                "di_subject_id": di_subject_id,
                "di_document_id": di_document_id,
                "actor_id": actor_id,
            },
        ).scalar_one()
        fact_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence_facts (
                    tenant_id, evidence_id, journey_id, field_key, value_type,
                    value_json, normalized_value, verification_status
                ) VALUES (
                    :tenant_id, :evidence_id, :journey_id, 'delivery_date', 'TEXT',
                    CAST(:value AS jsonb), '2026-08-16', 'NOT_VERIFIED'
                ) RETURNING evidence_fact_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "evidence_id": evidence_id,
                "journey_id": journey_id,
                "value": '"2026-08-16"',
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.deliveries (
                    tenant_id, journey_id, actual_delivery_status_code,
                    status_label_snapshot, status_source, recorded_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, 'DELIVERED_TEST',
                    'Delivered', 'EVIDENCE', :actor_id
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "actor_id": actor_id},
        )

    def connection_override():
        with engine.begin() as connection:
            yield connection

    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=actor_id,
        tenant_id=tenant_id,
        permissions=(
            "audit.finding.read",
            "audit.finding.create",
            "audit.finding.update",
            "audit.finding.resolve",
        ),
    )
    try:
        client = TestClient(app, raise_server_exceptions=False)
        url = f"/v1/tenants/{tenant_id}/journeys/{journey_id}/findings"
        created = client.post(
            url,
            json={
                "findingTypeCode": "DOCUMENT_MISMATCH",
                "severity": "MEDIUM",
                "title": "Delivery date evidence requires review",
                "expectedSummary": "Delivery date should match supporting records",
                "observedSummary": "Evidence date requires review",
                "evidence": [
                    {
                        "evidenceId": str(evidence_id),
                        "evidenceFactId": str(fact_id),
                        "linkagePurpose": "Observed delivery date",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["evidence"][0]["evidenceId"] == str(evidence_id)
        assert body["evidence"][0]["evidenceFactId"] == str(fact_id)

        resolved = client.patch(
            f"{url}/{body['auditFindingId']}",
            json={
                "findingStatus": "RESOLVED",
                "resolutionReason": "Reviewed and documented",
            },
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["findingStatus"] == "RESOLVED"
        assert len(client.get(url).json()) == 1

        with engine.begin() as connection:
            delivery_status = connection.execute(
                text(
                    """
                    SELECT actual_delivery_status_code
                    FROM auditcore.deliveries
                    WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).scalar_one()
        assert delivery_status == "DELIVERED_TEST"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
