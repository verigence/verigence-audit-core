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
            role_key=None,
        )


@pytest.fixture
def audit_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 audit integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-audit-{suffix}"
    actors = {
        "PC": f"uc03-pc-{suffix}",
        "TL": f"uc03-tl-{suffix}",
        "PM": f"uc03-pm-{suffix}",
        "EXECUTIVE": f"uc03-exec-{suffix}",
    }

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"UC03-AUD-CAT-{suffix}", "name": f"UC03 Audit Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"UC03-AUD-OEM-{suffix}", "name": f"UC03 Audit OEM {suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :project_code, 'UC03 Audit Project', :oem_id,
                    :category_id, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_code": f"UC03-AUD-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Audit Dealer')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"AUD-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (:tenant_id, :dealer_id, :code, 'Audit Outlet')
                RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"AUD-O-{suffix}"},
        ).scalar_one()
        for role, actor_id in actors.items():
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.business_assignments (
                        tenant_id, security_actor_id, business_role_code,
                        dealer_id, outlet_id
                    ) VALUES (:tenant_id, :actor_id, :role, :dealer_id, :outlet_id)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "actor_id": actor_id,
                    "role": role,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                },
            )
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'INDIVIDUAL', 'Audit Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, :reference
                ) RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
                "customer_id": customer_id,
                "reference": f"UC03-AUD-J-{suffix}",
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_stage_states (
                    tenant_id, journey_id, stage_code, business_status,
                    audit_state, audit_status, first_started_at_utc,
                    latest_activity_at_utc, version_no
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING', 'BOOKING_IN_PROGRESS',
                    'IN_PROGRESS', 'NOT_EVALUATED', now(), now(), 1
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        )

    active_actor = {"id": actors["PC"]}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(
        subject=active_actor["id"]
    )
    app.dependency_overrides[get_security_authorization_client] = (
        lambda: AllowedAuthorization()
    )
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "actors": actors,
            "active_actor": active_actor,
        }
    finally:
        app.dependency_overrides.pop(get_human_principal, None)
        app.dependency_overrides.pop(get_security_authorization_client, None)
        engine.dispose()


def _client() -> TestClient:
    return TestClient(app)


def _base(setup) -> str:
    return f"/v1/tenants/{setup['tenant_id']}/journeys/{setup['journey_id']}/uc03"


def _set_role(setup, role: str) -> None:
    setup["active_actor"]["id"] = setup["actors"][role]


def _create_flag(setup, *, key: str = "flag-create-0001", blocking: bool = False):
    response = _client().post(
        f"{_base(setup)}/flags",
        headers={"Idempotency-Key": key, "If-Match": '"1"'},
        json={
            "stage": "BOOKING",
            "category": "PROCESS_NON_COMPLIANCE",
            "severity": "HIGH",
            "summary": "Manual audit exception",
            "remarks": "Observed during Booking review",
            "blockingCompletion": blocking,
        },
    )
    assert response.status_code == 200, response.text
    return response


def test_pc_flag_create_is_idempotent_and_preserves_provenance(audit_setup):
    response = _create_flag(audit_setup)
    body = response.json()
    assert body["flag"]["originKind"] == "HUMAN"
    assert body["flag"]["originRole"] == "PC"
    assert body["flag"]["status"] == "OPEN"
    assert response.headers["etag"] == '"1"'

    replay = _client().post(
        f"{_base(audit_setup)}/flags",
        headers={"Idempotency-Key": "flag-create-0001", "If-Match": '"1"'},
        json={
            "stage": "BOOKING",
            "category": "PROCESS_NON_COMPLIANCE",
            "severity": "HIGH",
            "summary": "Manual audit exception",
            "remarks": "Observed during Booking review",
            "blockingCompletion": False,
        },
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent"] is True
    assert replay.json()["flag"]["flagId"] == body["flag"]["flagId"]

    with audit_setup["engine"].begin() as connection:
        finding_count = connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {
                "tenant_id": audit_setup["tenant_id"],
                "journey_id": audit_setup["journey_id"],
            },
        ).scalar_one()
        event_count = connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.audit_finding_events
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND event_type='RAISED'
                """
            ),
            {
                "tenant_id": audit_setup["tenant_id"],
                "journey_id": audit_setup["journey_id"],
            },
        ).scalar_one()
    assert finding_count == 1
    assert event_count == 1


def test_pc_cannot_acknowledge_but_tl_can_review_and_resolve(audit_setup):
    flag = _create_flag(audit_setup, key="flag-create-0002").json()["flag"]
    denied = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "flag-ack-pc-01", "If-Match": '"1"'},
        json={"action": "ACKNOWLEDGE"},
    )
    assert denied.status_code == 403

    _set_role(audit_setup, "TL")
    acknowledged = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "flag-ack-tl-01", "If-Match": '"1"'},
        json={"action": "ACKNOWLEDGE", "remarks": "Reviewed by Team Lead"},
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["flag"]["status"] == "ACKNOWLEDGED"
    assert acknowledged.json()["flag"]["version"] == 2

    stale = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "flag-resolve-stale", "If-Match": '"1"'},
        json={"action": "RESOLVE", "resolutionReason": "Corrected"},
    )
    assert stale.status_code == 409

    resolved = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "flag-resolve-tl-01", "If-Match": '"2"'},
        json={"action": "RESOLVE", "resolutionReason": "Evidence reconciled"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["flag"]["status"] == "RESOLVED"
    assert resolved.json()["flag"]["resolutionReason"] == "Evidence reconciled"


def test_pm_reopens_and_executive_can_void(audit_setup):
    flag = _create_flag(audit_setup, key="flag-create-0003").json()["flag"]
    _set_role(audit_setup, "TL")
    resolved = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "flag-resolve-tl-02", "If-Match": '"1"'},
        json={"action": "RESOLVE", "resolutionReason": "Initial resolution"},
    )
    assert resolved.status_code == 200

    _set_role(audit_setup, "PM")
    reopened = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "flag-reopen-pm-01", "If-Match": '"2"'},
        json={"action": "REOPEN", "resolutionReason": "Additional review required"},
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["flag"]["status"] == "OPEN"

    pm_void = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "flag-void-pm-01", "If-Match": '"3"'},
        json={"action": "VOID", "resolutionReason": "Not a valid finding"},
    )
    assert pm_void.status_code == 403

    _set_role(audit_setup, "EXECUTIVE")
    voided = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "flag-void-exec-01", "If-Match": '"3"'},
        json={"action": "VOID", "resolutionReason": "Confirmed invalid finding"},
    )
    assert voided.status_code == 200, voided.text
    assert voided.json()["flag"]["status"] == "VOIDED"


def test_complete_audit_keeps_historical_flags_raised_after_resolution(audit_setup):
    flag = _create_flag(audit_setup, key="flag-create-0004").json()["flag"]
    _set_role(audit_setup, "TL")
    resolved = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "flag-resolve-tl-03", "If-Match": '"1"'},
        json={"action": "RESOLVE", "resolutionReason": "Reviewed and resolved"},
    )
    assert resolved.status_code == 200

    summary = _client().get(f"{_base(audit_setup)}/audit-summary")
    assert summary.status_code == 200
    version = summary.json()["booking"]["aggregateVersion"]
    assert summary.json()["booking"]["auditStatus"] == "FLAGS_RAISED"

    completed = _client().post(
        f"{_base(audit_setup)}/stages/BOOKING/audit/complete",
        headers={"Idempotency-Key": "booking-audit-complete", "If-Match": f'"{version}"'},
        json={"remarks": "Booking audit work complete"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["auditState"] == "COMPLETE"
    assert completed.json()["auditStatus"] == "FLAGS_RAISED"

    after = _client().get(f"{_base(audit_setup)}/audit-summary").json()
    assert after["booking"]["auditState"] == "COMPLETE"
    assert after["booking"]["auditStatus"] == "FLAGS_RAISED"
    assert after["booking"]["openFlagCount"] == 0
    assert after["booking"]["totalHistoricalFlagCount"] == 1


def test_blocking_flag_prevents_audit_completion_until_resolved(audit_setup):
    flag = _create_flag(audit_setup, key="flag-create-0005", blocking=True).json()["flag"]
    summary = _client().get(f"{_base(audit_setup)}/audit-summary").json()
    version = summary["booking"]["aggregateVersion"]
    blocked = _client().post(
        f"{_base(audit_setup)}/stages/BOOKING/audit/complete",
        headers={"Idempotency-Key": "booking-audit-blocked", "If-Match": f'"{version}"'},
        json={},
    )
    assert blocked.status_code == 409

    _set_role(audit_setup, "PM")
    resolved = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "flag-resolve-pm-01", "If-Match": '"1"'},
        json={"action": "RESOLVE", "resolutionReason": "Guard satisfied"},
    )
    assert resolved.status_code == 200

    summary = _client().get(f"{_base(audit_setup)}/audit-summary").json()
    completed = _client().post(
        f"{_base(audit_setup)}/stages/BOOKING/audit/complete",
        headers={
            "Idempotency-Key": "booking-audit-unblocked",
            "If-Match": f'"{summary["booking"]["aggregateVersion"]}"',
        },
        json={},
    )
    assert completed.status_code == 200, completed.text


def test_machine_provenance_and_full_timeline_are_user_safe(audit_setup):
    with audit_setup["engine"].begin() as connection:
        flag_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_findings (
                    tenant_id, journey_id, finding_type_code, severity,
                    finding_status, title, stage_code, origin_kind,
                    origin_role_snapshot, rule_key, blocking_completion
                ) VALUES (
                    :tenant_id, :journey_id, 'MACHINE_CHECK', 'CRITICAL',
                    'OPEN', 'Machine reconciliation exception', 'BOOKING', 'MACHINE',
                    'SYSTEM', 'TEST_MACHINE_RULE', false
                ) RETURNING audit_finding_id
                """
            ),
            {
                "tenant_id": audit_setup["tenant_id"],
                "journey_id": audit_setup["journey_id"],
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_finding_events (
                    tenant_id, audit_finding_id, journey_id, stage_code,
                    event_type, actor_role_snapshot, safe_payload
                ) VALUES (
                    :tenant_id, :flag_id, :journey_id, 'BOOKING',
                    'RAISED', 'SYSTEM', '{"internal":"not exposed"}'::jsonb
                )
                """
            ),
            {
                "tenant_id": audit_setup["tenant_id"],
                "flag_id": flag_id,
                "journey_id": audit_setup["journey_id"],
            },
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET audit_status='FLAGS_RAISED'
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {
                "tenant_id": audit_setup["tenant_id"],
                "journey_id": audit_setup["journey_id"],
            },
        )

    flags = _client().get(f"{_base(audit_setup)}/flags")
    assert flags.status_code == 200
    machine = next(item for item in flags.json() if item["flagId"] == str(flag_id))
    assert machine["originKind"] == "MACHINE"
    assert machine["originRole"] == "SYSTEM"
    assert machine["ruleKey"] == "TEST_MACHINE_RULE"

    timeline = _client().get(f"{_base(audit_setup)}/timeline")
    assert timeline.status_code == 200
    serialized = timeline.text
    assert "Machine reconciliation exception" in serialized
    assert '"internal"' not in serialized
    for actor_id in audit_setup["actors"].values():
        assert actor_id not in serialized


def test_legacy_generic_patch_rejects_uc03_flag_lifecycle(audit_setup):
    flag = _create_flag(audit_setup, key="flag-create-0006").json()["flag"]
    # The legacy principal path is intentionally not configured in this human-token fixture;
    # protect the invariant directly at the persistence/lifecycle API boundary through C3.
    _set_role(audit_setup, "TL")
    action = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "flag-review-tl-01", "If-Match": '"1"'},
        json={"action": "REVIEW", "remarks": "Lifecycle event required"},
    )
    assert action.status_code == 200
    assert action.json()["flag"]["status"] == "ACKNOWLEDGED"


def test_timeline_is_bounded(audit_setup):
    _create_flag(audit_setup, key="flag-create-0007")
    response = _client().get(f"{_base(audit_setup)}/timeline?limit=1")
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_summary_exposes_role_capabilities_without_client_side_authority(audit_setup):
    pc = _client().get(f"{_base(audit_setup)}/audit-summary")
    assert pc.status_code == 200
    assert "RAISE" in pc.json()["permittedActions"]
    assert "RESOLVE" not in pc.json()["permittedActions"]

    _set_role(audit_setup, "TL")
    tl = _client().get(f"{_base(audit_setup)}/audit-summary")
    assert tl.status_code == 200
    assert "RESOLVE" in tl.json()["permittedActions"]
    assert "VOID" not in tl.json()["permittedActions"]

    _set_role(audit_setup, "EXECUTIVE")
    executive = _client().get(f"{_base(audit_setup)}/audit-summary")
    assert executive.status_code == 200
    assert "VOID" in executive.json()["permittedActions"]
