from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import uuid4

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
                    :tenant_id, :profile_id, 1, 'DRAFT', CURRENT_DATE - 1
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
                ),
                (
                    :tenant_id, :profile_version_id,
                    'NDC', 'NO_DUES_CERTIFICATE', 'DELIVERY',
                    'OPTIONAL', '{}'::jsonb, 30
                )
                """
            ),
            {"tenant_id": tenant_id, "profile_version_id": profile_version_id},
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.document_requirement_profile_versions
                SET lifecycle_status = 'PUBLISHED'
                WHERE tenant_id = :tenant_id
                  AND document_requirement_profile_version_id = :profile_version_id
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
            "customer_id": customer_id,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _booking_url(setup, suffix: str) -> str:
    return (
        f"/v1/tenants/{setup['tenant_id']}/journeys/{setup['journey_id']}"
        f"/booking/{suffix}"
    )


def _journey_url(setup, suffix: str) -> str:
    return (
        f"/v1/tenants/{setup['tenant_id']}/journeys/{setup['journey_id']}"
        f"/{suffix.lstrip('/')}"
    )


def _documents_url(setup, suffix: str = "") -> str:
    base = (
        f"/v1/tenants/{setup['tenant_id']}/journeys/{setup['journey_id']}"
        "/stages/BOOKING/documents"
    )
    return f"{base}/{suffix}" if suffix else base


def _headers(key: str, version: int) -> dict[str, str]:
    return {"Idempotency-Key": key, "If-Match": f'"{version}"'}


def _insert_completed_evidence(setup, requirement_key: str = "BOOKING_DOCKET"):
    with setup["engine"].begin() as connection:
        requirement_id = connection.execute(
            text(
                """
                SELECT journey_document_requirement_id
                FROM auditcore.journey_document_requirements
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND requirement_key=:requirement_key
                """
            ),
            {
                "tenant_id": setup["tenant_id"],
                "journey_id": setup["journey_id"],
                "requirement_key": requirement_key,
            },
        ).scalar_one()
        evidence_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id,
                    journey_document_requirement_id,
                    di_subject_id, di_document_id,
                    document_type_key, evidence_purpose, process_area,
                    processing_status_cache, cache_updated_at_utc,
                    linked_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, :customer_id,
                    :requirement_id,
                    :subject_id, :document_id,
                    :document_type_key, 'UC03_TEST', 'BOOKING',
                    'COMPLETED', now(), :actor_id
                ) RETURNING evidence_id
                """
            ),
            {
                "tenant_id": setup["tenant_id"],
                "journey_id": setup["journey_id"],
                "customer_id": setup["customer_id"],
                "requirement_id": requirement_id,
                "subject_id": uuid4(),
                "document_id": uuid4(),
                "document_type_key": requirement_key,
                "actor_id": setup["actor_id"],
            },
        ).scalar_one()
    return evidence_id


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

    replay = client.put(
        _documents_url(setup, "BOOKING_DOCKET"),
        headers=_headers("doc-assess-0002", 1),
        json={"answer": "YES", "remarks": "Physical document reviewed."},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == payload


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


def test_booking_capture_recalculates_conditional_document_applicability(
    booking_document_setup,
) -> None:
    setup = booking_document_setup
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post(
        _booking_url(setup, "start"),
        headers=_headers("c1-app-start", 0),
    ).status_code == 200

    captured = client.put(
        _journey_url(setup, "capture/EXCHANGE_TAKEN"),
        headers=_headers("c1-app-capture", 1),
        json={"value": True},
    )
    assert captured.status_code == 200, captured.text
    assert captured.json()["aggregateVersion"] == 2
    assert captured.json()["applicabilityChanges"][0]["requirementKey"] == "TRADE_IN_RC"
    assert captured.json()["applicabilityChanges"][0]["applicabilityState"] == "APPLICABLE"

    listed = client.get(_documents_url(setup))
    assert listed.status_code == 200, listed.text
    trade_in = next(item for item in listed.json() if item["requirementKey"] == "TRADE_IN_RC")
    assert trade_in["applicabilityState"] == "APPLICABLE"
    assert "exchangeTaken=Yes" in trade_in["applicabilityReason"]

    with setup["engine"].begin() as connection:
        details = connection.execute(
            text(
                """
                SELECT details
                FROM auditcore.trade_in_cases
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": setup["journey_id"]},
        ).scalar_one()
    assert details["exchangeTaken"] is True


def test_corrected_extraction_proposal_preserves_machine_original(
    booking_document_setup,
) -> None:
    setup = booking_document_setup
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post(
        _booking_url(setup, "start"),
        headers=_headers("c1-proposal-start", 0),
    ).status_code == 200
    evidence_id = _insert_completed_evidence(setup)
    proposal_id = uuid4()

    with setup["engine"].begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_capture_proposals (
                    tenant_id, capture_proposal_id, journey_id, stage_code,
                    field_key, source_evidence_id, source_evidence_fact_id,
                    source_fact_version, source_document_type_key, value_source,
                    proposed_value, confidence_score
                ) VALUES (
                    :tenant_id, :proposal_id, :journey_id, 'BOOKING',
                    'customer_name', :evidence_id, 'customer_name',
                    1, 'booking_form', 'EXTRACTION',
                    '{"value":"Machine Customer"}'::jsonb, 0.72
                )
                """
            ),
            {
                "tenant_id": setup["tenant_id"],
                "proposal_id": proposal_id,
                "journey_id": setup["journey_id"],
                "evidence_id": evidence_id,
            },
        )

    corrected = client.post(
        _journey_url(setup, f"extraction-proposals/{proposal_id}/correct"),
        headers=_headers("c1-proposal-correct", 1),
        json={"acceptedValue": "Corrected Customer"},
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["status"] == "CORRECTED"
    assert corrected.json()["proposedValue"] == "Machine Customer"
    assert corrected.json()["acceptedValue"] == "Corrected Customer"
    assert corrected.json()["aggregateVersion"] == 2

    with setup["engine"].begin() as connection:
        proposal = connection.execute(
            text(
                """
                SELECT proposed_value, accepted_value, proposal_status,
                       owning_domain_key
                FROM auditcore.journey_capture_proposals
                WHERE tenant_id=:tenant_id AND capture_proposal_id=:proposal_id
                """
            ),
            {"tenant_id": setup["tenant_id"], "proposal_id": proposal_id},
        ).mappings().one()
        customer_name = connection.execute(
            text(
                """
                SELECT display_name FROM auditcore.customers
                WHERE tenant_id=:tenant_id AND customer_id=:customer_id
                """
            ),
            {"tenant_id": setup["tenant_id"], "customer_id": setup["customer_id"]},
        ).scalar_one()
    assert proposal["proposed_value"] == {"value": "Machine Customer"}
    assert proposal["accepted_value"] == {"value": "Corrected Customer"}
    assert proposal["proposal_status"] == "CORRECTED"
    assert proposal["owning_domain_key"] == "CUSTOMER"
    assert customer_name == "Corrected Customer"


def test_normal_booking_close_allows_nonblocking_human_flag(
    booking_document_setup,
) -> None:
    setup = booking_document_setup
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post(
        _booking_url(setup, "start"),
        headers=_headers("c1-close-start", 0),
    ).status_code == 200

    exchange = client.put(
        _journey_url(setup, "capture/EXCHANGE_TAKEN"),
        headers=_headers("c1-close-exchange", 1),
        json={"value": False},
    )
    assert exchange.status_code == 200, exchange.text
    assert exchange.json()["aggregateVersion"] == 2

    na_response = client.put(
        _documents_url(setup, "TRADE_IN_RC"),
        headers=_headers("c1-close-na", 2),
        json={"answer": "NA", "remarks": "No exchange in this Booking."},
    )
    assert na_response.status_code == 200, na_response.text
    assert na_response.json()["aggregateVersion"] == 3

    evidence_id = _insert_completed_evidence(setup)
    docket = client.put(
        _documents_url(setup, "BOOKING_DOCKET"),
        headers=_headers("c1-close-docket", 3),
        json={"answer": "YES", "evidenceId": str(evidence_id)},
    )
    assert docket.status_code == 200, docket.text
    assert docket.json()["requirementStatus"] == "SATISFIED"
    assert docket.json()["aggregateVersion"] == 4

    flag = client.post(
        _journey_url(setup, "flags"),
        headers=_headers("c1-close-flag", 4),
        json={
            "category": "PHYSICAL_OBSERVATION",
            "severity": "MEDIUM",
            "summary": "Physical process observation",
            "remarks": "Visible for review but not configured as a completion blocker.",
            "evidenceIds": [],
        },
    )
    assert flag.status_code == 200, flag.text
    assert flag.json()["aggregateVersion"] == 5

    workspace = client.get(_journey_url(setup, "uc03-workspace"))
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["completion"]["ready"] is True
    assert workspace.json()["completion"]["blockingFlagCount"] == 0
    assert workspace.json()["flagSummary"]["openCount"] == 1

    closed = client.post(
        _booking_url(setup, "close-ready"),
        headers=_headers("c1-close-ready", 5),
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["businessStatus"] == "BOOKING_CLOSED"
    assert closed.json()["closureDisposition"] == "PROCEED_TO_DELIVERY"
    assert closed.json()["auditState"] == "COMPLETE"
    assert closed.json()["auditStatus"] == "FLAGS_RAISED"
    assert closed.json()["aggregateVersion"] == 6