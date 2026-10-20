from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationDecision,
    get_security_authorization_client,
)


@dataclass
class _AllowAllAuthorization:
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def check_user_permission(
        self,
        *,
        user_id: str,
        tenant_id: str,
        permission_key: str,
    ) -> SecurityAuthorizationDecision:
        self.calls.append((user_id, tenant_id, permission_key))
        return SecurityAuthorizationDecision(
            allowed=True,
            reason_code="AUTHORIZED",
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key="PC",
        )


@pytest.fixture
def uc03_create_booking_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 Create Booking integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-cb-{suffix}"
    actor_id = f"uc03-cb-pc-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"UC03-CB-CAT-{suffix}", "name": f"UC03 Create Booking Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"UC03-CB-OEM-{suffix}", "name": f"UC03 Create Booking OEM {suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :project_code, 'UC03 Create Booking Project', :oem_id,
                    :category_id, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_code": f"UC03-CB-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Create Booking Dealer')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"CBD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (:tenant_id, :dealer_id, :code, 'Create Booking Outlet')
                RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"CBO-{suffix}"},
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

    authorization = _AllowAllAuthorization()
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: authorization
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "outlet_id": outlet_id,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _journey_count(setup) -> int:
    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        with engine.begin() as connection:
            set_tenant_context(connection, setup["tenant_id"])
            return int(connection.execute(
                text("SELECT COUNT(*) FROM auditcore.journeys WHERE tenant_id=:t"), {"t": setup["tenant_id"]},
            ).scalar_one())
    finally:
        engine.dispose()


def test_the_old_create_booking_is_refused_and_creates_nothing(uc03_create_booking_setup) -> None:
    """Closed 2026-10-20 (uc03_legacy_capture_guard). Found live 19 Oct: the old
    Web Overview's "Capture new booking" created journey 9d1c307c here and the
    old whole-file upload then sent a 5-page PDF to DI as ONE document, so its
    PAN and Aadhaar were never read. Bookings start on Booking & Delivery
    (POST /p2/.../journeys) only."""
    setup = uc03_create_booking_setup
    client = TestClient(app, raise_server_exceptions=False)
    before = _journey_count(setup)

    refused = client.post(
        f"/v1/tenants/{setup['tenant_id']}/uc03/bookings",
        headers={"Idempotency-Key": "create-booking-closed-001"},
        json={"outletId": str(setup["outlet_id"])},
    )

    assert refused.status_code == 410, refused.text
    assert refused.json()["errorCode"] == "VAC-UC03-LEGACY-CAPTURE-CLOSED"
    assert "Booking & Delivery" in refused.json()["detail"]
    assert _journey_count(setup) == before
