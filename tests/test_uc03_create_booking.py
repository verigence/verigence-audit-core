from __future__ import annotations

import os
from dataclasses import dataclass, field
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


def test_create_booking_folds_in_the_same_checklist_a_separate_capture_local_call_would_return(
    uc03_create_booking_setup,
) -> None:
    """The workspace used to need a second, separate round trip
    (GET /booking/capture-local) after POST /bookings just to paint its
    checklist/counters -- reported live as a visible lag on a brand-new
    booking ("it takes some seconds for the above counters to appear").
    create_booking now computes and returns that same snapshot inline. This
    proves the folded-in value is exactly what a separate read would still
    independently compute for the same journey -- the fold changes when the
    read happens, not what it returns.

    GET /booking/capture-local no longer exists (Phase 4 unification --
    replaced by GET /uc03/documents/capture-local, which covers both
    stages). declarations/canContinue don't exist on that unified response
    either -- confirmed dead in every frontend consumer (no caller of
    setBookingCaptureV2Declaration, no reader of canContinue/declarations),
    so the comparison here is narrowed to what still exists on both sides:
    each BOOKING-stage requirement's shared fields.
    """
    setup = uc03_create_booking_setup
    client = TestClient(app, raise_server_exceptions=False)

    created = client.post(
        f"/v1/tenants/{setup['tenant_id']}/uc03/bookings",
        headers={"Idempotency-Key": "create-booking-fold-001"},
        json={"outletId": str(setup["outlet_id"]), "customerName": "Fold Test Customer"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    journey_id = UUID(body["journeyId"])

    assert "booking" in body
    booking = body["booking"]
    assert booking["journeyId"] == str(journey_id)
    assert booking["phase"] == "BOOKING"
    assert booking["uploads"] == []
    assert isinstance(booking["requirements"], list)
    assert len(booking["requirements"]) > 0
    assert isinstance(booking["canContinue"], bool)

    separate_read = client.get(
        f"/v2/tenants/{setup['tenant_id']}/journeys/{journey_id}/uc03/documents/capture-local",
    )
    assert separate_read.status_code == 200, separate_read.text
    shared_fields = (
        "requirementKey", "label", "documentTypeKey", "requirementLevel",
        "conditionKey", "applicabilityState", "state", "document", "canView", "canDelete",
    )

    def _shared(requirement: dict) -> dict:
        return {key: requirement[key] for key in shared_fields}

    booking_requirements = [_shared(item) for item in booking["requirements"]]
    unified_booking_requirements = [
        _shared(item)
        for item in separate_read.json()["requirements"]
        if item["stageCode"] == "BOOKING"
    ]
    assert booking_requirements == unified_booking_requirements


def test_create_booking_replay_still_returns_the_booking_snapshot(
    uc03_create_booking_setup,
) -> None:
    """An idempotent replay's stored response_body predates this field for
    any request recorded before this change -- the snapshot must be computed
    fresh on every call, not read back from the stored idempotency record."""
    setup = uc03_create_booking_setup
    client = TestClient(app, raise_server_exceptions=False)

    first = client.post(
        f"/v1/tenants/{setup['tenant_id']}/uc03/bookings",
        headers={"Idempotency-Key": "create-booking-fold-002"},
        json={"outletId": str(setup["outlet_id"]), "customerName": "Replay Test Customer"},
    )
    assert first.status_code == 201, first.text

    replay = client.post(
        f"/v1/tenants/{setup['tenant_id']}/uc03/bookings",
        headers={"Idempotency-Key": "create-booking-fold-002"},
        json={"outletId": str(setup["outlet_id"]), "customerName": "Replay Test Customer"},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()
    assert "booking" in replay.json()
