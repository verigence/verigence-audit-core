from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationDecision,
    get_security_authorization_client,
)


@dataclass
class AllowedAuthorization:
    def check_user_permission(
        self,
        *,
        user_id: str,
        tenant_id: str,
        permission_key: str,
    ) -> SecurityAuthorizationDecision:
        return SecurityAuthorizationDecision(
            allowed=True,
            reason_code="AUTHORIZED",
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key="PC",
        )


@pytest.fixture
def booking_document_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 document assessment tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-doc-{suffix}"
    actor_id = f"uc03-doc-pc-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"UC03-DOC-CAT-{suffix}", "name": f"UC03 Doc Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"UC03-DOC-OEM-{suffix}", "name": f"UC03 Doc OEM {suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :project_code, 'UC03 Document Project', :oem_id,
                    :category_id, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_code": f"UC03-DOC-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Document Dealer')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"DD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (:tenant_id, :dealer_id, :code, 'Document Outlet')
                RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"DO-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code,
                    dealer_id, outlet_id
                ) VALUES (:tenant_id, :actor_id, 'PC', :dealer_id, :outlet_id)
                """
            ),
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
            },
        )
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id,
                    customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id,
                    'INDIVIDUAL', 'Document Customer'
                ) RETURNING customer_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        ).scalar_one()
        profile_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.document_requirement_profiles (
                    tenant_id, profile_code, profile_name
                ) VALUES (:tenant_id, :profile_code, 'UC03 Booking Documents')
                RETURNING document_requirement_profile_id
                """
            ),
            {"tenant_id": tenant_id, "profile_code": f"UC03-DOC-PROFILE-{suffix}"},
        ).scalar_one()
        profile_version_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.document_requirement_profile_versions (
                    tenant_id, document_requirement_profile_id, version_no,
                    lifecycle_status, effective_from
                ) VALUES (
                    :tenant_id, :profile_id, 1, 'PUBLISHED', CURRENT_DATE - 1
                )
                RETURNING document_requirement_profile_version_id
                """
            ),
            {"tenant_id": tenant_id, "profile_id": profile_id},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.document_requirement_items (
                    tenant_id, document_requirement_profile_version_id,
                    requirement_key, document_type_key, process_area,
                    requirement_level, condition_config, sort_order
                ) VALUES
                (
                    :tenant_id, :profile_version_id,
                    'BOOKING_DOCKET', 'BOOKING_DOCKET', 'BOOKING',
                    'REQUIRED', '{}'::jsonb, 10
                ),
                (
                    :tenant_id, :profile_version_id,
                    'TRADE_IN_RC', 'TRADE_IN_RC', 'BOOKING',
                    'CONDITIONAL',
                    '{"conditionKey":"exchangeTaken"}'::jsonb, 20
                )
                """
            ),
            {"tenant_id": tenant_id, "profile_version_id": profile_version_id},
        )
        journey_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id,
                    journey_reference, document_requirement_profile_version_id
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, :customer_id,
                    :reference, :profile_version_id
                ) RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
                "customer_id": customer_id,
                "reference": f"UC03-DOC-J-{suffix}",
                "profile_version_id": profile_version_id,
            },
        ).scalar_one()

    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowedAuthorization()
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "journey_id": journey_id,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _booking_url(setup, suffix: str) -> str:
    return (
        f"/v1/tenants/{setup['tenant_id']}/journeys/{setup['journey_id']}"
        f"/booking/{suffix}"
    )


def _documents_url(setup, suffix: str = "") -> str:
    base = (
        f"/v1/tenants/{setup['tenant_id']}/journeys/{setup['journey_id']}"
        "/stages/BOOKING/documents"
    )
    return f"{base}/{suffix}" if suffix else base


def _headers(key: str, version: int) -> dict[str, str]:
    return {"Idempotency-Key": key, "If-Match": f'"{version}"'}


def test_booking_start_snapshots_profile_requirements(booking_document_setup) -> None:
    setup = booking_document_setup
    client = TestClient(app, raise_server_exceptions=False)

    started = client.post(
        _booking_url(setup, "start"),
        headers=_headers("doc-start-0001", 0),
    )
    assert started.status_code == 200, started.text
    assert started.json()["aggregateVersion"] == 1

    listed = client.get(_documents_url(setup))
    assert listed.status_code == 200, listed.text
    by_key = {item["requirementKey"]: item for item in listed.json()}
    assert set(by_key) == {"BOOKING_DOCKET", "TRADE_IN_RC"}
    assert by_key["BOOKING_DOCKET"]["applicabilityState"] == "APPLICABLE"
    assert by_key["BOOKING_DOCKET"]["answer"] == "UNANSWERED"
    assert by_key["TRADE_IN_RC"]["applicabilityState"] == "UNRESOLVED"

    with setup["engine"].begin() as connection:
        snapshots = connection.execute(
            text(
                """
                SELECT requirement_key, requirement_level, requirement_status,
                       condition_snapshot
                FROM auditcore.journey_document_requirements
                WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                ORDER BY requirement_key
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": setup["journey_id"]},
        ).mappings().all()
    assert [row["requirement_key"] for row in snapshots] == [
        "BOOKING_DOCKET",
        "TRADE_IN_RC",
    ]
    assert all(row["requirement_status"] == "PENDING" for row in snapshots)


def test_required_assessment_moves_booking_to_in_progress_and_is_idempotent(
    booking_document_setup,
) -> None:
    setup = booking_document_setup
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post(
        _booking_url(setup, "start"),
        headers=_headers("doc-start-0002", 0),
    ).status_code == 200

    assessed = client.put(
        _documents_url(setup, "BOOKING_DOCKET"),
        headers=_headers("doc-assess-0002", 1),
        json={"answer": "YES", "remarks": "Physical document reviewed."},
    )
    assert assessed.status_code == 200, assessed.text
    payload = assessed.json()
    assert payload["answer"] == "YES"
    assert payload["requirementStatus"] == "PENDING"
    assert payload["aggregateVersion"] == 2
    assert assessed.headers["etag"] == '"2"'

    replay = client.put(
        _documents_url(setup, "BOOKING_DOCKET"),
        headers=_headers("doc-assess-0002", 1),
        json={"answer": "YES", "remarks": "Physical document reviewed."},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == payload

    with setup["engine"].begin() as connection:
        stage = connection.execute(
            text(
                """
                SELECT business_status, audit_state, version_no
                FROM auditcore.journey_stage_states
                WHERE tenant_id = :tenant_id
                  AND journey_id = :journey_id
                  AND stage_code = 'BOOKING'
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": setup["journey_id"]},
        ).mappings().one()
        event_count = connection.execute(
            text(
                """
                SELECT count(*)
                FROM auditcore.journey_workflow_events
                WHERE tenant_id = :tenant_id
                  AND journey_id = :journey_id
                  AND event_type = 'DOCUMENT_ASSESSMENT_RECORDED'
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": setup["journey_id"]},
        ).scalar_one()
    assert stage == {
        "business_status": "BOOKING_IN_PROGRESS",
        "audit_state": "IN_PROGRESS",
        "version_no": 2,
    }
    assert event_count == 1


def test_unresolved_conditional_requirement_cannot_be_assessed(
    booking_document_setup,
) -> None:
    setup = booking_document_setup
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post(
        _booking_url(setup, "start"),
        headers=_headers("doc-start-0003", 0),
    ).status_code == 200

    response = client.put(
        _documents_url(setup, "TRADE_IN_RC"),
        headers=_headers("doc-assess-0003", 1),
        json={"answer": "NO"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["errorCode"] == "VAC-CONFLICT-007"

    with setup["engine"].begin() as connection:
        assessment_count = connection.execute(
            text(
                """
                SELECT count(*)
                FROM auditcore.journey_document_assessments
                WHERE tenant_id = :tenant_id
                  AND journey_id = :journey_id
                  AND requirement_key = 'TRADE_IN_RC'
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": setup["journey_id"]},
        ).scalar_one()
    assert assessment_count == 0
