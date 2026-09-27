from __future__ import annotations

import os
from decimal import Decimal
from dataclasses import dataclass
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationDecision,
    get_security_authorization_client,
)
from audit_core import uc03_p2_stage


@dataclass
class AllowAuthorization:
    def check_user_permission(self, *, user_id: str, tenant_id: str, permission_key: str):
        return SecurityAuthorizationDecision(
            allowed=True,
            reason_code="AUTHORIZED",
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key="PC",
        )


@pytest.fixture
def p2_api_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for P2 API integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-p2-api-{suffix}"
    actor_id = f"pc-p2-api-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, 'P2 Test Category')
                RETURNING product_category_id
                """
            ),
            {"code": f"P2C-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, 'P2 Test OEM')
                RETURNING oem_id
                """
            ),
            {"code": f"P2O-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :project_code, 'P2 API Test Project', :oem_id,
                    :category_id, CURRENT_DATE, 'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_code": f"P2API-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'P2 Dealer')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"P2D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'P2 Outlet'
                )
                RETURNING outlet_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "code": f"P2O-{suffix}",
            },
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code,
                    display_name, legal_name_status, mobile_last4
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'INDIVIDUAL',
                    'P2 Customer', 'UNVERIFIED', '1234'
                )
                RETURNING customer_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
            },
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, :customer_id
                )
                RETURNING journey_id
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
                    :tenant_id, :actor_id, 'PC', :dealer_id, :outlet_id
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

    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAuthorization()
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "oem_id": oem_id,
            "category_id": category_id,
        }
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, tenant_id)
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM auditcore.oems WHERE oem_id=:id"),
                {"id": oem_id},
            )
            connection.execute(
                text("DELETE FROM auditcore.product_categories WHERE product_category_id=:id"),
                {"id": category_id},
            )
        engine.dispose()


def test_p2_journey_task_and_overview_reads_are_type_safe(p2_api_setup) -> None:
    """Regression for the DEV 500s caused by UUID/text comparisons.

    These are real Postgres-backed requests. A bad UNION/join comparison is
    rejected by PostgreSQL even when no task rows exist, so this test catches
    the exact class of failure that escaped the original source-inspection tests.
    """
    setup = p2_api_setup
    client = TestClient(app, raise_server_exceptions=False)
    tenant = setup["tenant_id"]
    journey = setup["journey_id"]

    journeys = client.get(f"/p2/v1/tenants/{tenant}/journeys")
    assert journeys.status_code == 200, journeys.text
    journey_body = journeys.json()
    assert [item["journey_id"] for item in journey_body["items"]] == [str(journey)]

    tasks = client.get(f"/p2/v1/tenants/{tenant}/tasks")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["items"] == []

    overview = client.get(f"/p2/v1/tenants/{tenant}/journeys/{journey}/overview")
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["journey"]["journey_id"] == str(journey)
    assert body["stage"]["bookingCompletionState"] == "IN_PROGRESS"
    assert body["stage"]["deliveryCompletionState"] == "IN_PROGRESS"
    assert body["tasks"]["open"] == 0
    assert body["findings"]["open"] == 0


def test_p2_task_api_returns_readable_journey_context(p2_api_setup) -> None:
    setup = p2_api_setup
    tenant = setup["tenant_id"]
    journey = setup["journey_id"]

    with setup["engine"].begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_tasks (
                    tenant_id, journey_id, task_type, category, origin_kind,
                    source_type, source_code, dedupe_key, title, description,
                    reference, severity, priority, assigned_role_code,
                    allowed_actions, completion_protocol
                ) VALUES (
                    :tenant_id, :journey_id, 'REVIEW_DOCUMENT',
                    'DOCUMENT_VERIFICATION', 'SYSTEM', 'DOCUMENT',
                    'WRONG_DOCUMENT', :dedupe_key, 'Review document',
                    'Confirm the classified document type.',
                    '{"documentTypeKey":"booking_form","fieldKey":"customer_name"}'::jsonb,
                    'MEDIUM', 'HIGH', 'PC',
                    '["REVIEW_DOCUMENT","ADD_COMMENT"]'::jsonb,
                    'MACHINE_VERIFIED'
                )
                """
            ),
            {
                "tenant_id": tenant,
                "journey_id": journey,
                "dedupe_key": f"test:{journey}",
            },
        )

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(f"/p2/v1/tenants/{tenant}/tasks")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    task = items[0]
    assert task["customer_name"] == "P2 Customer"
    assert task["dealer_name"] == "P2 Dealer"
    assert task["outlet_name"] == "P2 Outlet"
    assert task["journey_id"] == str(journey)
    assert task["reference"]["documentTypeKey"] == "booking_form"



def test_p2_stage_reopens_booking_after_delivery_has_started(
    p2_api_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correction that invalidates Booking must reopen Booking even after
    Delivery has started; the P2 projection must never get stuck in DELIVERY_*.
    """
    setup = p2_api_setup
    tenant = setup["tenant_id"]
    journey = setup["journey_id"]

    monkeypatch.setattr(
        uc03_p2_stage,
        "_minimum_booking_amount",
        lambda connection, *, tenant_id: Decimal("50000"),
    )
    monkeypatch.setattr(
        uc03_p2_stage,
        "_booking_receipt_total",
        lambda connection, *, tenant_id, journey_id: Decimal("50000"),
    )
    monkeypatch.setattr(
        uc03_p2_stage,
        "_manual_verification_pending",
        lambda connection, *, tenant_id, journey_id: 0,
    )
    monkeypatch.setattr(
        uc03_p2_stage,
        "_delivery_started",
        lambda connection, *, tenant_id, journey_id: True,
    )
    monkeypatch.setattr(
        uc03_p2_stage,
        "_document_extracted",
        lambda connection, *, tenant_id, journey_id, document_types: (True, 1),
    )

    with setup["engine"].begin() as connection:
        result = uc03_p2_stage.recompute_journey_stage(
            connection,
            tenant_id=tenant,
            journey_id=journey,
        )
        assert result["bookingCompletionState"] == "COMPLETE"
        assert result["stage"] == "DELIVERY_DOCUMENT_UPLOAD"

    def document_state(connection, *, tenant_id, journey_id, document_types):
        del connection, tenant_id, journey_id
        if "pan_card" in document_types:
            return False, 0
        return True, 1

    monkeypatch.setattr(uc03_p2_stage, "_document_extracted", document_state)

    with setup["engine"].begin() as connection:
        reopened = uc03_p2_stage.recompute_journey_stage(
            connection,
            tenant_id=tenant,
            journey_id=journey,
        )
        persisted = connection.execute(
            text(
                """
                SELECT current_stage
                FROM auditcore.p2_journey_runtime
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {"tenant_id": tenant, "journey_id": journey},
        ).scalar_one()

    assert reopened["bookingCompletionState"] == "IN_PROGRESS"
    assert reopened["stage"] == "BOOKING_DOCUMENT_UPLOAD"
    assert persisted == "BOOKING_DOCUMENT_UPLOAD"
