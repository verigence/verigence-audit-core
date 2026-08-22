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
class ControlledAuthorization:
    allowed: bool = True
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
            allowed=self.allowed,
            reason_code="AUTHORIZED" if self.allowed else "PERMISSION_DENIED",
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key="PC" if self.allowed else None,
        )


@pytest.fixture
def uc03_booking_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 Booking integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-booking-{suffix}"
    actor_id = f"uc03-booking-pc-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"UC03-B-CAT-{suffix}", "name": f"UC03 Booking Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"UC03-B-OEM-{suffix}", "name": f"UC03 Booking OEM {suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :project_code, 'UC03 Booking Project', :oem_id,
                    :category_id, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_code": f"UC03-B-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Booking Dealer')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"BD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (:tenant_id, :dealer_id, :code, 'Booking Outlet')
                RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"BO-{suffix}"},
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

        journey_ids: list[UUID] = []
        for index in range(5):
            customer_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.customers (
                        tenant_id, dealer_id, outlet_id,
                        customer_type_code, display_name
                    ) VALUES (
                        :tenant_id, :dealer_id, :outlet_id,
                        'INDIVIDUAL', :display_name
                    ) RETURNING customer_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                    "display_name": f"Booking Customer {index}",
                },
            ).scalar_one()
            journey_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.journeys (
                        tenant_id, dealer_id, outlet_id, customer_id,
                        journey_reference
                    ) VALUES (
                        :tenant_id, :dealer_id, :outlet_id, :customer_id,
                        :reference
                    ) RETURNING journey_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                    "customer_id": customer_id,
                    "reference": f"UC03-B-J-{index}-{suffix}",
                },
            ).scalar_one()
            journey_ids.append(journey_id)

    authorization = ControlledAuthorization()
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: authorization
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "journey_ids": journey_ids,
            "authorization": authorization,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _url(setup, journey_id: UUID, command: str) -> str:
    return f"/v1/tenants/{setup['tenant_id']}/journeys/{journey_id}/booking/{command}"


def _headers(key: str, version: int) -> dict[str, str]:
    return {
        "Idempotency-Key": key,
        "If-Match": f'"{version}"',
    }


def test_start_booking_is_idempotent_and_creates_immutable_event(uc03_booking_setup) -> None:
    setup = uc03_booking_setup
    journey_id = setup["journey_ids"][0]
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        _url(setup, journey_id, "start"),
        headers=_headers("booking-start-001", 0),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["businessStatus"] == "BOOKING_STARTED"
    assert payload["auditState"] == "NOT_STARTED"
    assert payload["auditStatus"] == "NOT_EVALUATED"
    assert payload["aggregateVersion"] == 1
    assert response.headers["etag"] == '"1"'

    replay = client.post(
        _url(setup, journey_id, "start"),
        headers=_headers("booking-start-001", 0),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == payload

    with setup["engine"].begin() as connection:
        event_count = connection.execute(
            text(
                """
                SELECT count(*)
                FROM auditcore.journey_workflow_events
                WHERE tenant_id = :tenant_id
                  AND journey_id = :journey_id
                  AND event_type = 'BOOKING_STARTED'
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": journey_id},
        ).scalar_one()
    assert event_count == 1
    assert setup["authorization"].calls[-1] == (
        setup["actor_id"],
        setup["tenant_id"],
        "audit.journey.update",
    )


def test_booking_command_rejects_stale_if_match(uc03_booking_setup) -> None:
    setup = uc03_booking_setup
    journey_id = setup["journey_ids"][1]
    client = TestClient(app, raise_server_exceptions=False)

    started = client.post(
        _url(setup, journey_id, "start"),
        headers=_headers("booking-start-002", 0),
    )
    assert started.status_code == 200, started.text

    stale = client.post(
        _url(setup, journey_id, "cancel"),
        headers=_headers("booking-cancel-stale", 0),
        json={"closeReasonCode": "CUSTOMER_CANCELLED"},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["errorCode"] == "VAC-CONFLICT-005"


def test_close_no_delivery_validates_reason_and_preserves_audit_state(uc03_booking_setup) -> None:
    setup = uc03_booking_setup
    journey_id = setup["journey_ids"][2]
    client = TestClient(app, raise_server_exceptions=False)

    started = client.post(
        _url(setup, journey_id, "start"),
        headers=_headers("booking-start-003", 0),
    )
    assert started.status_code == 200, started.text

    missing_remarks = client.post(
        _url(setup, journey_id, "close-no-delivery"),
        headers=_headers("booking-close-003a", 1),
        json={"closeReasonCode": "OTHER"},
    )
    assert missing_remarks.status_code == 422, missing_remarks.text

    closed = client.post(
        _url(setup, journey_id, "close-no-delivery"),
        headers=_headers("booking-close-003b", 1),
        json={"closeReasonCode": "OTHER", "remarks": "Customer will not proceed."},
    )
    assert closed.status_code == 200, closed.text
    payload = closed.json()
    assert payload["businessStatus"] == "BOOKING_CLOSED"
    assert payload["closureDisposition"] == "NO_DELIVERY"
    assert payload["closeReasonCode"] == "OTHER"
    assert payload["auditState"] == "NOT_STARTED"
    assert payload["auditStatus"] == "NOT_EVALUATED"
    assert payload["aggregateVersion"] == 2


def test_cancel_booking_is_terminal_for_phase_one(uc03_booking_setup) -> None:
    setup = uc03_booking_setup
    journey_id = setup["journey_ids"][3]
    client = TestClient(app, raise_server_exceptions=False)

    started = client.post(
        _url(setup, journey_id, "start"),
        headers=_headers("booking-start-004", 0),
    )
    assert started.status_code == 200, started.text

    cancelled = client.post(
        _url(setup, journey_id, "cancel"),
        headers=_headers("booking-cancel-004", 1),
        json={"closeReasonCode": "CUSTOMER_CANCELLED"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["businessStatus"] == "BOOKING_CANCELLED"
    assert cancelled.json()["aggregateVersion"] == 2

    duplicate_after_cancel = client.post(
        _url(setup, journey_id, "mark-duplicate"),
        headers=_headers("booking-duplicate-004", 2),
        json={},
    )
    assert duplicate_after_cancel.status_code == 409, duplicate_after_cancel.text
    assert duplicate_after_cancel.json()["errorCode"] == "VAC-CONFLICT-004"


def test_mark_duplicate_sets_terminal_status_and_mandatory_high_flag(uc03_booking_setup) -> None:
    setup = uc03_booking_setup
    journey_id = setup["journey_ids"][4]
    client = TestClient(app, raise_server_exceptions=False)

    started = client.post(
        _url(setup, journey_id, "start"),
        headers=_headers("booking-start-005", 0),
    )
    assert started.status_code == 200, started.text

    duplicate = client.post(
        _url(setup, journey_id, "mark-duplicate"),
        headers=_headers("booking-duplicate-005", 1),
        json={"remarks": "Confirmed duplicate during Booking review."},
    )
    assert duplicate.status_code == 200, duplicate.text
    payload = duplicate.json()
    assert payload["businessStatus"] == "DUPLICATE_BOOKING"
    assert payload["auditStatus"] == "FLAGS_RAISED"
    assert payload["closeReasonCode"] == "DUPLICATE_BOOKING"
    assert payload["aggregateVersion"] == 2
    assert payload["flagId"] is not None

    replay = client.post(
        _url(setup, journey_id, "mark-duplicate"),
        headers=_headers("booking-duplicate-005", 1),
        json={"remarks": "Confirmed duplicate during Booking review."},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == payload

    with setup["engine"].begin() as connection:
        finding = connection.execute(
            text(
                """
                SELECT severity, finding_status, finding_type_code,
                       stage_code, origin_kind, origin_role_snapshot, rule_key
                FROM auditcore.audit_findings
                WHERE tenant_id = :tenant_id
                  AND audit_finding_id = :finding_id
                """
            ),
            {"tenant_id": setup["tenant_id"], "finding_id": UUID(payload["flagId"])},
        ).mappings().one()
        finding_event_count = connection.execute(
            text(
                """
                SELECT count(*)
                FROM auditcore.audit_finding_events
                WHERE tenant_id = :tenant_id
                  AND audit_finding_id = :finding_id
                  AND event_type = 'RAISED'
                """
            ),
            {"tenant_id": setup["tenant_id"], "finding_id": UUID(payload["flagId"])},
        ).scalar_one()
        workflow_events = set(
            connection.execute(
                text(
                    """
                    SELECT event_type
                    FROM auditcore.journey_workflow_events
                    WHERE tenant_id = :tenant_id
                      AND journey_id = :journey_id
                    """
                ),
                {"tenant_id": setup["tenant_id"], "journey_id": journey_id},
            ).scalars()
        )
        duplicate_findings = connection.execute(
            text(
                """
                SELECT count(*)
                FROM auditcore.audit_findings
                WHERE tenant_id = :tenant_id
                  AND journey_id = :journey_id
                  AND rule_key = 'WF_DUPLICATE_BOOKING'
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": journey_id},
        ).scalar_one()

    assert finding == {
        "severity": "HIGH",
        "finding_status": "OPEN",
        "finding_type_code": "DUPLICATE_BOOKING",
        "stage_code": "BOOKING",
        "origin_kind": "MACHINE",
        "origin_role_snapshot": "SYSTEM",
        "rule_key": "WF_DUPLICATE_BOOKING",
    }
    assert finding_event_count == 1
    assert "BOOKING_MARKED_DUPLICATE" in workflow_events
    assert "FLAG_RAISED" in workflow_events
    assert duplicate_findings == 1
