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
class AllowAuthorization:
    calls: int = 0

    def check_user_permission(self, *, user_id: str, tenant_id: str, permission_key: str):
        self.calls += 1
        return SecurityAuthorizationDecision(
            allowed=True,
            reason_code="AUTHORIZED",
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key="PC",
        )


def _journey(connection, *, tenant_id: str, dealer_id, outlet_id, label: str):
    customer_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.customers (
                tenant_id, dealer_id, outlet_id, customer_type_code, display_name
            ) VALUES (
                :tenant_id, :dealer_id, :outlet_id, 'INDIVIDUAL', :label
            ) RETURNING customer_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "label": label,
        },
    ).scalar_one()
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.journeys (
                tenant_id, dealer_id, outlet_id, customer_id
            ) VALUES (
                :tenant_id, :dealer_id, :outlet_id, :customer_id
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


def _stage(connection, *, tenant_id: str, journey_id, stage: str, status: str):
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_stage_states (
                tenant_id, journey_id, stage_code, business_status,
                audit_state, audit_status
            ) VALUES (
                :tenant_id, :journey_id, :stage, :status,
                'IN_PROGRESS', 'NOT_EVALUATED'
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage": stage,
            "status": status,
        },
    )


@pytest.fixture
def landing_metrics_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 landing-metrics integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-metrics-{suffix}"
    actor_id = f"user-uc03-metrics-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name) RETURNING product_category_id
                """
            ),
            {"code": f"MCAT-{suffix}", "name": "Metrics Category"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name) RETURNING oem_id
                """
            ),
            {"code": f"MOEM-{suffix}", "name": "Metrics OEM"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :code, 'Metrics Project', :oem_id,
                    :category_id, CURRENT_DATE, 'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"M-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )

        dealer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Scoped Dealer') RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Scoped Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"O-{suffix}"},
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

        booking_one = _journey(
            connection,
            tenant_id=tenant_id,
            dealer_id=dealer_id,
            outlet_id=outlet_id,
            label="Booking One",
        )
        booking_two = _journey(
            connection,
            tenant_id=tenant_id,
            dealer_id=dealer_id,
            outlet_id=outlet_id,
            label="Booking Two",
        )
        delivery_one = _journey(
            connection,
            tenant_id=tenant_id,
            dealer_id=dealer_id,
            outlet_id=outlet_id,
            label="Delivery One",
        )
        _stage(
            connection,
            tenant_id=tenant_id,
            journey_id=booking_one,
            stage="BOOKING",
            status="BOOKING_STARTED",
        )
        _stage(
            connection,
            tenant_id=tenant_id,
            journey_id=booking_two,
            stage="BOOKING",
            status="BOOKING_IN_PROGRESS",
        )
        _stage(
            connection,
            tenant_id=tenant_id,
            journey_id=delivery_one,
            stage="DELIVERY",
            status="DELIVERY_IN_PROGRESS",
        )
        for title in ("Flag one", "Flag two"):
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.audit_findings (
                        tenant_id, journey_id, severity, finding_status, title
                    ) VALUES (
                        :tenant_id, :journey_id, 'HIGH', 'OPEN', :title
                    )
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": booking_two, "title": title},
            )

        hidden_dealer = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Hidden Dealer') RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"HD-{suffix}"},
        ).scalar_one()
        hidden_outlet = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Hidden Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": hidden_dealer, "code": f"HO-{suffix}"},
        ).scalar_one()
        hidden = _journey(
            connection,
            tenant_id=tenant_id,
            dealer_id=hidden_dealer,
            outlet_id=hidden_outlet,
            label="Hidden Delivery",
        )
        _stage(
            connection,
            tenant_id=tenant_id,
            journey_id=hidden,
            stage="DELIVERY",
            status="DELIVERY_IN_PROGRESS",
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_findings (
                    tenant_id, journey_id, severity, finding_status, title
                ) VALUES (
                    :tenant_id, :journey_id, 'CRITICAL', 'OPEN', 'Hidden flag'
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": hidden},
        )

    authorization = AllowAuthorization()
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: authorization
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "authorization": authorization,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_landing_metrics_are_scope_filtered_and_live_authorized(landing_metrics_setup) -> None:
    setup = landing_metrics_setup
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"/v1/tenants/{setup['tenant_id']}/uc03/landing-metrics")

    assert response.status_code == 200
    assert response.json() == {
        "bookingsInProgress": 2,
        "deliveryInProgress": 1,
        "needsAttention": 1,
        "auditFlags": 2,
        "auditInProgress": 3,
    }
    assert setup["authorization"].calls == 1
