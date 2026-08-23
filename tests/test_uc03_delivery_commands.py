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
def delivery_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 Delivery integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-delivery-{suffix}"
    actor_id = f"uc03-delivery-pc-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {
                "code": f"UC03-DL-CAT-{suffix}",
                "name": f"UC03 Delivery Category {suffix}",
            },
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {
                "code": f"UC03-DL-OEM-{suffix}",
                "name": f"UC03 Delivery OEM {suffix}",
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :project_code, 'UC03 Delivery Project', :oem_id,
                    :category_id, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_code": f"UC03-DL-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Delivery Dealer')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"DL-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (:tenant_id, :dealer_id, :code, 'Delivery Outlet')
                RETURNING outlet_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "code": f"DL-O-{suffix}",
            },
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

        profile_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.document_requirement_profiles (
                    tenant_id, profile_code, profile_name
                ) VALUES (:tenant_id, :code, 'UC03 Delivery Documents')
                RETURNING document_requirement_profile_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"UC03-DL-PROFILE-{suffix}"},
        ).scalar_one()
        profile_version_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.document_requirement_profile_versions (
                    tenant_id, document_requirement_profile_id, version_no,
                    lifecycle_status, effective_from
                ) VALUES (:tenant_id, :profile_id, 1, 'DRAFT', CURRENT_DATE - 1)
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
                    'NDC', 'NO_DUES_CERTIFICATE', 'DELIVERY',
                    'REQUIRED', '{}'::jsonb, 10
                ),
                (
                    :tenant_id, :profile_version_id,
                    'CAR_PICTURES', 'CAR_PICTURES', 'DELIVERY',
                    'REQUIRED', '{}'::jsonb, 20
                ),
                (
                    :tenant_id, :profile_version_id,
                    'TRADE_IN_RC_DELIVERY', 'TRADE_IN_RC', 'DELIVERY',
                    'CONDITIONAL', '{"conditionKey":"exchangeTaken"}'::jsonb, 30
                )
                """
            ),
            {"tenant_id": tenant_id, "profile_version_id": profile_version_id},
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.document_requirement_profile_versions
                SET lifecycle_status='PUBLISHED'
                WHERE tenant_id=:tenant_id
                  AND document_requirement_profile_version_id=:profile_version_id
                """
            ),
            {"tenant_id": tenant_id, "profile_version_id": profile_version_id},
        )

        journey_ids: list[UUID] = []
        for index in range(7):
            customer_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.customers (
                        tenant_id, dealer_id, outlet_id,
                        customer_type_code, display_name
                    ) VALUES (
                        :tenant_id, :dealer_id, :outlet_id,
                        'INDIVIDUAL', :name
                    ) RETURNING customer_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                    "name": f"Delivery Customer {index}",
                },
            ).scalar_one()
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
                    "reference": f"UC03-DL-J-{index}-{suffix}",
                    "profile_version_id": profile_version_id,
                },
            ).scalar_one()
            journey_ids.append(journey_id)

        booking_states = [
            (journey_ids[0], "BOOKING_CLOSED", "PROCEED_TO_DELIVERY"),
            (journey_ids[1], "BOOKING_IN_PROGRESS", None),
            (journey_ids[2], "BOOKING_CANCELLED", None),
            (journey_ids[3], "BOOKING_CLOSED", "PROCEED_TO_DELIVERY"),
            (journey_ids[4], "BOOKING_CLOSED", "PROCEED_TO_DELIVERY"),
            (journey_ids[5], "BOOKING_CLOSED", "PROCEED_TO_DELIVERY"),
            (journey_ids[6], "BOOKING_CLOSED", "NO_DELIVERY"),
        ]
        for journey_id, status, disposition in booking_states:
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.journey_stage_states (
                        tenant_id, journey_id, stage_code, business_status,
                        closure_disposition, audit_state, audit_status,
                        first_started_at_utc, latest_activity_at_utc, version_no
                    ) VALUES (
                        :tenant_id, :journey_id, 'BOOKING', :status,
                        :disposition, 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "journey_id": journey_id,
                    "status": status,
                    "disposition": disposition,
                },
            )

        connection.execute(
            text(
                """
                INSERT INTO auditcore.vehicle_records (
                    tenant_id, journey_id, vin, chassis_number,
                    source_kind
                ) VALUES (
                    :tenant_id, :journey_id,
                    'MA1AB2CD3EF456789', 'MA1AB2CD3EF456789',
                    'SOURCE_SYSTEM'
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_ids[4]},
        )

    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(
        subject=actor_id
    )
    app.dependency_overrides[get_security_authorization_client] = (
        lambda: AllowedAuthorization()
    )
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "journey_ids": journey_ids,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _delivery_url(setup, journey_id: UUID, suffix: str) -> str:
    return (
        f"/v1/tenants/{setup['tenant_id']}/journeys/{journey_id}"
        f"/delivery/{suffix.lstrip('/')}"
    )


def _document_url(setup, journey_id: UUID, requirement_key: str = "") -> str:
    base = (
        f"/v1/tenants/{setup['tenant_id']}/journeys/{journey_id}"
        "/stages/DELIVERY/documents"
    )
    return f"{base}/{requirement_key}" if requirement_key else base


def _headers(key: str, version: int) -> dict[str, str]:
    return {"Idempotency-Key": key, "If-Match": f'"{version}"'}


def _start(client: TestClient, setup, journey_id: UUID, key: str):
    return client.post(
        _delivery_url(setup, journey_id, "start"),
        headers=_headers(key, 0),
    )


def test_clean_delivery_start_is_idempotent_and_records_business_history(
    delivery_setup,
) -> None:
    setup = delivery_setup
    journey_id = setup["journey_ids"][0]
    client = TestClient(app, raise_server_exceptions=False)

    started = _start(client, setup, journey_id, "delivery-start-clean-001")
    assert started.status_code == 200, started.text
    payload = started.json()
    assert payload["businessStatus"] == "DELIVERY_STARTED"
    assert payload["auditState"] == "NOT_STARTED"
    assert payload["raisedFlagIds"] == []
    assert payload["aggregateVersion"] == 1
    assert started.headers["etag"] == '"1"'

    replay = _start(client, setup, journey_id, "delivery-start-clean-001")
    assert replay.status_code == 200, replay.text
    assert replay.json() == payload

    with setup["engine"].begin() as connection:
        statuses = connection.execute(
            text(
                """
                SELECT actual_delivery_status_code
                FROM auditcore.delivery_status_history
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                ORDER BY recorded_at_utc
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": journey_id},
        ).scalars().all()
    assert statuses == ["DELIVERY_STARTED"]


def test_delivery_start_with_incomplete_booking_flags_but_does_not_block(
    delivery_setup,
) -> None:
    setup = delivery_setup
    journey_id = setup["journey_ids"][1]
    client = TestClient(app, raise_server_exceptions=False)

    started = _start(client, setup, journey_id, "delivery-start-incomplete-001")
    assert started.status_code == 200, started.text
    payload = started.json()
    assert payload["businessStatus"] == "DELIVERY_STARTED"
    assert len(payload["raisedFlagIds"]) == 1

    with setup["engine"].begin() as connection:
        booking = connection.execute(
            text(
                """
                SELECT business_status
                FROM auditcore.journey_stage_states
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": journey_id},
        ).scalar_one()
        rule_key = connection.execute(
            text(
                """
                SELECT rule_key
                FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": journey_id},
        ).scalar_one()
    assert booking == "BOOKING_IN_PROGRESS"
    assert rule_key == "WF_BOOKING_INCOMPLETE_AT_DELIVERY_START"


def test_delivery_rejects_cancelled_and_no_delivery_booking(delivery_setup) -> None:
    setup = delivery_setup
    client = TestClient(app, raise_server_exceptions=False)

    cancelled = _start(
        client,
        setup,
        setup["journey_ids"][2],
        "delivery-start-cancelled-001",
    )
    assert cancelled.status_code == 409, cancelled.text

    no_delivery = _start(
        client,
        setup,
        setup["journey_ids"][6],
        "delivery-start-no-delivery-001",
    )
    assert no_delivery.status_code == 409, no_delivery.text


def test_non_intimation_raises_flag_and_delivery_continues(delivery_setup) -> None:
    setup = delivery_setup
    journey_id = setup["journey_ids"][3]
    client = TestClient(app, raise_server_exceptions=False)
    started = _start(client, setup, journey_id, "delivery-start-intimation-001")
    assert started.status_code == 200, started.text

    invalid = client.put(
        _delivery_url(setup, journey_id, "intimation"),
        headers=_headers("delivery-intimation-no-invalid", 1),
        json={"answer": "NO"},
    )
    assert invalid.status_code == 422, invalid.text

    recorded = client.put(
        _delivery_url(setup, journey_id, "intimation"),
        headers=_headers("delivery-intimation-no-001", 1),
        json={"answer": "NO", "reason": "Dealer did not notify PC before handover."},
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["answer"] == "NO"
    assert recorded.json()["flagId"] is not None
    assert recorded.json()["aggregateVersion"] == 2

    workspace = client.get(_delivery_url(setup, journey_id, "workspace"))
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["delivery"]["businessStatus"] == "DELIVERY_IN_PROGRESS"
    assert workspace.json()["intimation"]["answer"] == "NO"


def test_vin_mismatch_is_critical_flag_not_delivery_block(delivery_setup) -> None:
    setup = delivery_setup
    journey_id = setup["journey_ids"][4]
    client = TestClient(app, raise_server_exceptions=False)
    started = _start(client, setup, journey_id, "delivery-start-vin-001")
    assert started.status_code == 200, started.text

    observed = client.put(
        _delivery_url(setup, journey_id, "vehicle-observation"),
        headers=_headers("delivery-vin-observation-001", 1),
        json={"vin": "MA1AB2CD3EF456780"},
    )
    assert observed.status_code == 200, observed.text
    assert observed.json()["reconciliationStatus"] == "MISMATCH"
    assert observed.json()["flagId"] is not None

    completed = client.post(
        _delivery_url(setup, journey_id, "complete"),
        headers=_headers("delivery-complete-vin-001", 2),
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["businessStatus"] == "DELIVERY_COMPLETED"
    assert completed.json()["auditState"] == "IN_PROGRESS"

    with setup["engine"].begin() as connection:
        severity = connection.execute(
            text(
                """
                SELECT severity FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND rule_key='DL_VIN_RECONCILIATION'
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": journey_id},
        ).scalar_one()
    assert severity == "CRITICAL"


def test_document_no_and_unverified_payment_do_not_block_physical_completion(
    delivery_setup,
) -> None:
    setup = delivery_setup
    journey_id = setup["journey_ids"][5]
    client = TestClient(app, raise_server_exceptions=False)
    started = _start(client, setup, journey_id, "delivery-start-doc-payment-001")
    assert started.status_code == 200, started.text

    docs = client.get(_document_url(setup, journey_id))
    assert docs.status_code == 200, docs.text
    assert {item["requirementKey"] for item in docs.json()} >= {"NDC", "CAR_PICTURES"}

    assessed = client.put(
        _document_url(setup, journey_id, "NDC"),
        headers=_headers("delivery-doc-ndc-no-001", 1),
        json={"answer": "NO", "remarks": "NDC not available at physical handover."},
    )
    assert assessed.status_code == 200, assessed.text
    assert assessed.json()["answer"] == "NO"
    assert assessed.json()["flagId"] is not None
    assert assessed.json()["aggregateVersion"] == 2

    with setup["engine"].begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.payments (
                    tenant_id, journey_id, amount, currency_code,
                    payment_method_code, payment_reference
                ) VALUES (
                    :tenant_id, :journey_id, 50000, 'INR',
                    'BANK_TRANSFER', 'UTR-UNVERIFIED'
                )
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": journey_id},
        )

    completed = client.post(
        _delivery_url(setup, journey_id, "complete"),
        headers=_headers("delivery-complete-gaps-001", 2),
    )
    assert completed.status_code == 200, completed.text
    payload = completed.json()
    assert payload["businessStatus"] == "DELIVERY_COMPLETED"
    assert payload["auditState"] == "IN_PROGRESS"
    assert payload["auditStatus"] == "FLAGS_RAISED"
    assert len(payload["raisedFlagIds"]) >= 2

    late_answer = client.put(
        _document_url(setup, journey_id, "CAR_PICTURES"),
        headers=_headers("delivery-doc-late-no-001", 3),
        json={"answer": "NO", "remarks": "Captured after physical handover."},
    )
    assert late_answer.status_code == 200, late_answer.text
    assert late_answer.json()["aggregateVersion"] == 4
