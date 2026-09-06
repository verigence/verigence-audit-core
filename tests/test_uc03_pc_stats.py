from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


def _journey(connection, *, tenant_id, dealer_id, outlet_id, label):
    customer_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.customers (
                tenant_id, dealer_id, outlet_id, customer_type_code, display_name
            ) VALUES (:tenant_id, :dealer_id, :outlet_id, 'INDIVIDUAL', :label)
            RETURNING customer_id
            """
        ),
        {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id, "label": label},
    ).scalar_one()
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id)
            VALUES (:tenant_id, :dealer_id, :outlet_id, :customer_id)
            RETURNING journey_id
            """
        ),
        {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id, "customer_id": customer_id},
    ).scalar_one()


def _stage(connection, *, tenant_id, journey_id, stage, status, completed_days_ago=None):
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_stage_states (
                tenant_id, journey_id, stage_code, business_status,
                audit_state, audit_status, business_completed_at_utc
            ) VALUES (
                :tenant_id, :journey_id, :stage, :status,
                'NOT_STARTED', 'NOT_EVALUATED',
                now() - make_interval(days => CAST(:days_ago AS integer))
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage": stage,
            "status": status,
            "days_ago": completed_days_ago,
        },
    )


@pytest.fixture
def pc_stats_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 pc-stats integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-pcstats-{suffix}"
    actor_id = f"user-uc03-pcstats-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name)"
                " VALUES (:code, :name) RETURNING product_category_id"
            ),
            {"code": f"PSCAT-{suffix}", "name": "PC Stats Category"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name)"
                " VALUES (:code, :name) RETURNING oem_id"
            ),
            {"code": f"PSOEM-{suffix}", "name": "PC Stats OEM"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :code, 'PC Stats Project', :oem_id,
                    :category_id, CURRENT_DATE, 'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {"tenant_id": tenant_id, "code": f"PS-{suffix}", "oem_id": oem_id, "category_id": category_id},
        )

        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)"
                " VALUES (:tenant_id, :code, 'Scoped Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name)"
                " VALUES (:tenant_id, :dealer_id, :code, 'Scoped Outlet') RETURNING outlet_id"
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"O-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code, dealer_id, outlet_id
                ) VALUES (:tenant_id, :actor_id, 'PC', :dealer_id, :outlet_id)
                """
            ),
            {"tenant_id": tenant_id, "actor_id": actor_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        )

        booking_done = _journey(connection, tenant_id=tenant_id, dealer_id=dealer_id, outlet_id=outlet_id, label="Booking done")
        booking_open = _journey(connection, tenant_id=tenant_id, dealer_id=dealer_id, outlet_id=outlet_id, label="Booking open")
        delivery_old = _journey(connection, tenant_id=tenant_id, dealer_id=dealer_id, outlet_id=outlet_id, label="Delivery old")
        delivery_open = _journey(connection, tenant_id=tenant_id, dealer_id=dealer_id, outlet_id=outlet_id, label="Delivery open")

        _stage(connection, tenant_id=tenant_id, journey_id=booking_done, stage="BOOKING", status="BOOKING_CLOSED", completed_days_ago=2)
        _stage(connection, tenant_id=tenant_id, journey_id=booking_open, stage="BOOKING", status="BOOKING_IN_PROGRESS")
        _stage(connection, tenant_id=tenant_id, journey_id=delivery_old, stage="DELIVERY", status="DELIVERY_COMPLETED", completed_days_ago=40)
        _stage(connection, tenant_id=tenant_id, journey_id=delivery_open, stage="DELIVERY", status="DELIVERY_IN_PROGRESS")

        # Out-of-scope dealer/outlet — must never be counted.
        hidden_dealer = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)"
                " VALUES (:tenant_id, :code, 'Hidden Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"HD-{suffix}"},
        ).scalar_one()
        hidden_outlet = connection.execute(
            text(
                "INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name)"
                " VALUES (:tenant_id, :dealer_id, :code, 'Hidden Outlet') RETURNING outlet_id"
            ),
            {"tenant_id": tenant_id, "dealer_id": hidden_dealer, "code": f"HO-{suffix}"},
        ).scalar_one()
        hidden = _journey(connection, tenant_id=tenant_id, dealer_id=hidden_dealer, outlet_id=hidden_outlet, label="Hidden booking")
        _stage(connection, tenant_id=tenant_id, journey_id=hidden, stage="BOOKING", status="BOOKING_CLOSED", completed_days_ago=1)

    authorization = AllowAuthorization()
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: authorization
    try:
        yield {"engine": engine, "tenant_id": tenant_id, "authorization": authorization}
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_pc_stats_counts_completed_in_window_and_in_progress_now(pc_stats_setup) -> None:
    setup = pc_stats_setup
    client = TestClient(app, raise_server_exceptions=False)
    today = datetime.now(tz=UTC).date()

    week = client.get(
        f"/v1/tenants/{setup['tenant_id']}/uc03/pc-stats",
        params={"from": str(today - timedelta(days=7)), "to": str(today)},
    )
    assert week.status_code == 200
    assert week.json() == {
        "bookingsCompleted": 1,
        "deliveriesCompleted": 0,
        "bookingsInProgress": 1,
        "deliveriesInProgress": 1,
    }
    assert setup["authorization"].calls == 1

    wide = client.get(
        f"/v1/tenants/{setup['tenant_id']}/uc03/pc-stats",
        params={"from": str(today - timedelta(days=90)), "to": str(today)},
    )
    assert wide.status_code == 200
    assert wide.json()["deliveriesCompleted"] == 1
    assert wide.json()["bookingsCompleted"] == 1


def test_pc_stats_rejects_reversed_range(pc_stats_setup) -> None:
    setup = pc_stats_setup
    client = TestClient(app, raise_server_exceptions=False)
    today = datetime.now(tz=UTC).date()

    response = client.get(
        f"/v1/tenants/{setup['tenant_id']}/uc03/pc-stats",
        params={"from": str(today), "to": str(today - timedelta(days=1))},
    )
    assert response.status_code == 400
    assert response.json()["errorCode"] == "VAC-VAL-001"


def test_pc_stats_is_outlet_scoped(pc_stats_setup) -> None:
    setup = pc_stats_setup
    client = TestClient(app, raise_server_exceptions=False)
    today = datetime.now(tz=UTC).date()
    random_outlet = str(uuid4())

    response = client.get(
        f"/v1/tenants/{setup['tenant_id']}/uc03/pc-stats",
        params={
            "from": str(today - timedelta(days=90)),
            "to": str(today),
            "outletId": random_outlet,
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "bookingsCompleted": 0,
        "deliveriesCompleted": 0,
        "bookingsInProgress": 0,
        "deliveriesInProgress": 0,
    }
