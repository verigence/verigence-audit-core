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


def _create_flag(setup, *, key: str = "flag-create-0001"):
    # v1.1 correction: PC never raises a Finding either -- only TL/PM/
    # EXECUTIVE do. Post as TL, then restore whichever role the caller had
    # active (most callers rely on remaining PC immediately afterward).
    previous_actor = setup["active_actor"]["id"]
    _set_role(setup, "TL")
    try:
        response = _client().post(
            f"{_base(setup)}/flags",
            headers={"Idempotency-Key": key, "If-Match": '"1"'},
            json={
                "stage": "BOOKING",
                "category": "PROCESS_NON_COMPLIANCE",
                "severity": "HIGH",
                "summary": "Manual audit exception",
                "remarks": "Observed during Booking review",
            },
        )
    finally:
        setup["active_actor"]["id"] = previous_actor
    assert response.status_code == 200, response.text
    return response


def _create_flag_category(
    setup, *, category: str, key: str, severity: str = "HIGH", if_match: str = '"1"'
):
    previous_actor = setup["active_actor"]["id"]
    _set_role(setup, "TL")
    try:
        response = _client().post(
            f"{_base(setup)}/flags",
            headers={"Idempotency-Key": key, "If-Match": if_match},
            json={
                "stage": "BOOKING",
                "category": category,
                "severity": severity,
                "summary": f"{category} raised",
                "remarks": f"{category} observed during audit review",
            },
        )
    finally:
        setup["active_actor"]["id"] = previous_actor
    assert response.status_code == 200, response.text
    return response.json()["flag"]


# ── finding routing / SLA / adjudication ────────────────────────────────────────

def test_violation_flag_is_routed_to_tl_with_sla(audit_setup):
    # _create_flag_category posts as TL (PC can no longer raise) -- the
    # response's own permittedActions reflects TL's view, the role that
    # actually made this request, not PC's.
    flag = _create_flag_category(
        audit_setup, category="COMMERCIAL_EXCEPTION", key="route-violation-01"
    )
    assert flag["findingClass"] == "VIOLATION"
    assert flag["resolutionMode"] == "ADJUDICATED"
    assert flag["ownerRoleCode"] == "TL"
    assert flag["slaDueAtUtc"] is not None
    assert flag["escalationLevel"] == 0
    assert flag["overdue"] is False
    assert set(flag["permittedActions"]) == {
        "REMARK", "ACKNOWLEDGE", "CONFIRM_BREACH", "MARK_FALSE_POSITIVE",
        "TAKE_ACTION", "ESCALATE", "RESOLVE",
    }

    # PC has zero actions on a VIOLATION -- not even a comment.
    as_pc = _client().get(f"{_base(audit_setup)}/flags?stage=BOOKING").json()
    pc_view = next(item for item in as_pc if item["flagId"] == flag["flagId"])
    assert pc_view["permittedActions"] == []


def test_document_gap_flag_is_routed_to_pc(audit_setup):
    # Same reason as above -- this response reflects TL (the creator), not PC.
    flag = _create_flag_category(
        audit_setup, category="DOCUMENT_EXCEPTION", key="route-docgap-01"
    )
    assert flag["findingClass"] == "DOCUMENT_GAP"
    assert flag["resolutionMode"] == "SELF_SERVICE"
    assert flag["ownerRoleCode"] == "PC"
    assert set(flag["permittedActions"]) == {"REMARK", "RESOLVE"}

    # v1.1: PC never acts on a Finding directly, even a self-serve one -- it
    # normally closes itself once PC's auto-spawned Task is completed and
    # the underlying gap is actually fixed. TL/PM's RESOLVE here is a manual
    # override.
    as_pc = _client().get(f"{_base(audit_setup)}/flags?stage=BOOKING").json()
    pc_view = next(item for item in as_pc if item["flagId"] == flag["flagId"])
    assert pc_view["permittedActions"] == []


def test_document_gap_flag_auto_spawns_a_pc_task(audit_setup):
    """The other half of "a PC never opens a Finding": a self-serve gap
    (human- or machine-raised) auto-spawns a Task the instant it's raised,
    so PC's actual work surface is the Task, not the Finding."""
    flag = _create_flag_category(
        audit_setup, category="DOCUMENT_EXCEPTION", key="route-docgap-task-01"
    )
    with audit_setup["engine"].begin() as connection:
        task = connection.execute(
            text(
                """
                SELECT journey_id, daily_ops_run_id, task_type, assigned_role_code,
                       severity, task_status
                FROM auditcore.workflow_tasks
                WHERE tenant_id=:tenant_id AND related_finding_id=:flag_id
                """
            ),
            {"tenant_id": audit_setup["tenant_id"], "flag_id": UUID(flag["flagId"])},
        ).mappings().one()
    assert task["journey_id"] == audit_setup["journey_id"]
    assert task["daily_ops_run_id"] is None
    assert task["task_type"] == "AUTO_SELF_SERVE"
    assert task["assigned_role_code"] == "PC"
    assert task["severity"] == "HIGH"
    assert task["task_status"] == "READY"


def test_tl_accepts_violation_and_records_confirmed_breach(audit_setup):
    flag = _create_flag_category(
        audit_setup, category="COMMERCIAL_EXCEPTION", key="adj-accept-01"
    )
    _set_role(audit_setup, "TL")
    accepted = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "adj-accept-act-01", "If-Match": '"1"'},
        json={"action": "CONFIRM_BREACH", "resolutionReason": "Discount exceeds policy — breach"},
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()["flag"]
    assert body["status"] == "RESOLVED"
    assert body["disposition"] == "CONFIRMED_BREACH"


def test_tl_rejects_violation_and_records_not_a_breach(audit_setup):
    flag = _create_flag_category(
        audit_setup, category="COMMERCIAL_EXCEPTION", key="adj-reject-01"
    )
    _set_role(audit_setup, "TL")
    rejected = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "adj-reject-act-01", "If-Match": '"1"'},
        json={
            "action": "MARK_FALSE_POSITIVE",
            "resolutionReason": "Within approved deviation",
            "rejectionCategory": "NOT_APPLICABLE",
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["flag"]["disposition"] == "FALSE_POSITIVE"


def test_accept_is_rejected_for_a_document_gap(audit_setup):
    flag = _create_flag_category(
        audit_setup, category="DOCUMENT_EXCEPTION", key="adj-wrongclass-01"
    )
    _set_role(audit_setup, "TL")
    denied = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "adj-wrongclass-act-01", "If-Match": '"1"'},
        json={"action": "CONFIRM_BREACH", "resolutionReason": "n/a"},
    )
    assert denied.status_code == 403


def test_pc_cannot_resolve_a_violation(audit_setup):
    flag = _create_flag_category(
        audit_setup, category="COMMERCIAL_EXCEPTION", key="adj-pcblock-01"
    )
    denied = _client().post(
        f"{_base(audit_setup)}/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "adj-pcblock-act-01", "If-Match": '"1"'},
        json={"action": "RESOLVE", "resolutionReason": "n/a"},
    )
    assert denied.status_code == 403


# ── cross-journey review queue ──────────────────────────────────────────────────

def _queue(setup) -> str:
    return f"/v1/tenants/{setup['tenant_id']}/uc03/review-queue"


def test_review_queue_routes_by_role_and_supports_scope(audit_setup):
    violation = _create_flag_category(
        audit_setup, category="COMMERCIAL_EXCEPTION", key="q-violation-01", if_match='"1"'
    )
    doc_gap = _create_flag_category(
        audit_setup, category="DOCUMENT_EXCEPTION", key="q-docgap-01", if_match='"2"'
    )

    # PC sees the document gap (theirs), not the violation
    pc_all = _client().get(f"{_queue(audit_setup)}").json()
    pc_ids = {item["flagId"] for item in pc_all["items"]}
    assert doc_gap["flagId"] in pc_ids
    assert violation["flagId"] not in pc_ids
    assert "PC" in pc_all["roles"]

    # TL sees the violation (theirs) and can Accept / Reject it
    _set_role(audit_setup, "TL")
    tl_mine = _client().get(f"{_queue(audit_setup)}?scope=MINE").json()["items"]
    tl_item = next(item for item in tl_mine if item["flagId"] == violation["flagId"])
    assert tl_item["isMine"] is True
    assert tl_item["version"] == 1
    assert "CONFIRM_BREACH" in tl_item["permittedActions"]
    assert doc_gap["flagId"] not in {item["flagId"] for item in tl_mine}

    summary = _client().get(f"{_queue(audit_setup)}/summary").json()
    assert summary["mine"] >= 1
    assert summary["byClass"].get("VIOLATION", 0) >= 1


def test_review_queue_tasks_are_opt_in(audit_setup):
    """includeTasks defaults False so the endpoint's live behavior doesn't
    change under a frontend that doesn't know about itemKind yet -- a
    genuinely distinct Task (TL_TAKE_ACTION, spawned by a TL's own verdict
    on a Violation) is invisible until asked for."""
    violation = _create_flag_category(
        audit_setup, category="COMMERCIAL_EXCEPTION", key="q-taskoptin-01"
    )
    _set_role(audit_setup, "TL")
    take_action = _client().post(
        f"{_base(audit_setup)}/flags/{violation['flagId']}/actions",
        headers={"Idempotency-Key": "q-taskoptin-act-01", "If-Match": '"1"'},
        json={"action": "TAKE_ACTION", "resolutionReason": "Please provide supporting evidence.", "severity": "HIGH"},
    )
    assert take_action.status_code == 200, take_action.text

    default_items = _client().get(f"{_queue(audit_setup)}").json()["items"]
    assert all(item["itemKind"] == "FINDING" for item in default_items)

    with_tasks = _client().get(f"{_queue(audit_setup)}?includeTasks=true").json()["items"]
    kinds_by_flag = {item["flagId"]: item["itemKind"] for item in with_tasks}
    assert kinds_by_flag[violation["flagId"]] == "FINDING"
    tasks = [item for item in with_tasks if item["itemKind"] == "EXECUTION_TASK"]
    assert len(tasks) == 1
    assert tasks[0]["relatedFindingId"] == violation["flagId"]
    assert tasks[0]["category"] == "TL_TAKE_ACTION"
    assert tasks[0]["permittedActions"] == []


def test_review_queue_never_duplicates_a_self_serve_gap_as_its_own_task(audit_setup):
    """AUTO_SELF_SERVE is always spawned 1:1 from the DATA_GAP/DOCUMENT_GAP
    finding it was raised for -- showing both was the same gap twice, once
    correctly classified (Missing Document) and once under a generic
    "Self-serve gap" label that told nobody anything new. Excluded from the
    queue entirely, even with includeTasks=true; the finding is still there."""
    doc_gap = _create_flag_category(
        audit_setup, category="DOCUMENT_EXCEPTION", key="q-noselfservedup-01"
    )

    with_tasks = _client().get(f"{_queue(audit_setup)}?includeTasks=true").json()["items"]
    kinds_by_flag = {item["flagId"]: item["itemKind"] for item in with_tasks}
    assert kinds_by_flag[doc_gap["flagId"]] == "FINDING"
    assert not [item for item in with_tasks if item["itemKind"] == "EXECUTION_TASK"]

    # The summary endpoint's byKind counts must reflect the same exclusion --
    # previously this inflated the Task Queue KPI tiles by exactly the
    # number of self-serve gaps present (each counted as both a FINDING and
    # an EXECUTION_TASK).
    summary_with_tasks = _client().get(f"{_queue(audit_setup)}/summary?includeTasks=true").json()
    assert summary_with_tasks["byKind"] == {"FINDING": 1}


def test_pc_cannot_raise_a_flag(audit_setup):
    """v1.1 correction: an earlier reading of "PC can't edit/update
    Findings" wrongly left RAISE open to PC. PC raises nothing -- every
    observation is TL/PM's to record, or the machine's."""
    denied = _client().post(
        f"{_base(audit_setup)}/flags",
        headers={"Idempotency-Key": "flag-pc-denied", "If-Match": '"1"'},
        json={
            "stage": "BOOKING",
            "category": "PROCESS_NON_COMPLIANCE",
            "severity": "HIGH",
            "summary": "Attempted PC-raised exception",
            "remarks": "Should never be recorded",
        },
    )
    assert denied.status_code == 403, denied.text
    with audit_setup["engine"].begin() as connection:
        finding_count = connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {"tenant_id": audit_setup["tenant_id"], "journey_id": audit_setup["journey_id"]},
        ).scalar_one()
    assert finding_count == 0


def test_tl_flag_create_is_idempotent_and_preserves_provenance(audit_setup):
    _set_role(audit_setup, "TL")
    response = _client().post(
        f"{_base(audit_setup)}/flags",
        headers={"Idempotency-Key": "flag-create-0001", "If-Match": '"1"'},
        json={
            "stage": "BOOKING",
            "category": "PROCESS_NON_COMPLIANCE",
            "severity": "HIGH",
            "summary": "Manual audit exception",
            "remarks": "Observed during Booking review",
        },
    )
    body = response.json()
    assert body["flag"]["originKind"] == "HUMAN"
    assert body["flag"]["originRole"] == "TL"
    assert body["flag"]["status"] == "OPEN"
    assert body["flag"]["blockingCompletion"] is False
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


def test_human_flag_cannot_self_declare_completion_guard(audit_setup):
    _set_role(audit_setup, "TL")
    response = _client().post(
        f"{_base(audit_setup)}/flags",
        headers={"Idempotency-Key": "flag-human-guard", "If-Match": '"1"'},
        json={
            "stage": "BOOKING",
            "category": "PROCESS_NON_COMPLIANCE",
            "severity": "HIGH",
            "summary": "Attempted manual completion guard",
            "blockingCompletion": True,
        },
    )
    assert response.status_code == 400, response.text
    with audit_setup["engine"].begin() as connection:
        finding_count = connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {
                "tenant_id": audit_setup["tenant_id"],
                "journey_id": audit_setup["journey_id"],
            },
        ).scalar_one()
    assert finding_count == 0


def test_human_flag_requires_non_blank_remarks(audit_setup):
    # Regression test: remarks used to be optional, and create_flag's own
    # execute() sets the finding's description straight from it -- a human
    # flag whose Remarks was left blank landed with nothing beyond its
    # one-line title to explain it. Required now, both a missing key and a
    # whitespace-only one.
    _set_role(audit_setup, "TL")
    missing = _client().post(
        f"{_base(audit_setup)}/flags",
        headers={"Idempotency-Key": "flag-no-remarks", "If-Match": '"1"'},
        json={
            "stage": "BOOKING",
            "category": "PROCESS_NON_COMPLIANCE",
            "severity": "HIGH",
            "summary": "Attempted flag with no remarks",
        },
    )
    assert missing.status_code in (400, 422), missing.text

    blank = _client().post(
        f"{_base(audit_setup)}/flags",
        headers={"Idempotency-Key": "flag-blank-remarks", "If-Match": '"1"'},
        json={
            "stage": "BOOKING",
            "category": "PROCESS_NON_COMPLIANCE",
            "severity": "HIGH",
            "summary": "Attempted flag with whitespace-only remarks",
            "remarks": "   ",
        },
    )
    assert blank.status_code in (400, 422), blank.text

    with audit_setup["engine"].begin() as connection:
        finding_count = connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {
                "tenant_id": audit_setup["tenant_id"],
                "journey_id": audit_setup["journey_id"],
            },
        ).scalar_one()
    assert finding_count == 0


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
    with audit_setup["engine"].begin() as connection:
        flag_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_findings (
                    tenant_id, journey_id, finding_type_code, severity,
                    finding_status, title, stage_code, origin_kind,
                    origin_role_snapshot, rule_key, blocking_completion
                ) VALUES (
                    :tenant_id, :journey_id, 'AUDIT_COMPLETION_GUARD', 'HIGH',
                    'OPEN', 'Configured completion guard', 'BOOKING', 'MACHINE',
                    'SYSTEM', 'TEST_COMPLETION_GUARD', true
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

    summary = _client().get(f"{_base(audit_setup)}/audit-summary")
    assert summary.status_code == 200, summary.text
    version = summary.json()["booking"]["aggregateVersion"]
    assert summary.json()["booking"]["blockingOpenFlagCount"] == 1
    blocked = _client().post(
        f"{_base(audit_setup)}/stages/BOOKING/audit/complete",
        headers={"Idempotency-Key": "booking-audit-blocked", "If-Match": f'"{version}"'},
        json={},
    )
    assert blocked.status_code == 409

    _set_role(audit_setup, "PM")
    resolved = _client().post(
        f"{_base(audit_setup)}/flags/{flag_id}/actions",
        headers={"Idempotency-Key": "flag-resolve-pm-01", "If-Match": '"1"'},
        json={"action": "RESOLVE", "resolutionReason": "Guard satisfied"},
    )
    assert resolved.status_code == 200

    summary = _client().get(f"{_base(audit_setup)}/audit-summary")
    assert summary.status_code == 200
    completed = _client().post(
        f"{_base(audit_setup)}/stages/BOOKING/audit/complete",
        headers={
            "Idempotency-Key": "booking-audit-unblocked",
            "If-Match": f'"{summary.json()["booking"]["aggregateVersion"]}"',
        },
        json={},
    )
    assert completed.status_code == 200, completed.text


def test_non_checklist_requirement_does_not_block_audit_completion(audit_setup):
    # bank_statement_extract (0070) is registered directly on the journey
    # (document_requirement_item_id=NULL) rather than sourced from the
    # published checklist profile -- it's just another document type DI can
    # classify against, not a "does this apply" declaration question. An
    # unanswered row like this must never block audit completion the way an
    # unanswered checklist item (document_requirement_item_id set) does.
    # (audit_setup's booking-start already snapshots it via the Booking
    # trigger -- this insert is belt-and-suspenders / ON CONFLICT DO NOTHING
    # so the test still proves the point even if that snapshot changes.)
    with audit_setup["engine"].begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_requirements (
                    tenant_id, journey_id, document_requirement_item_id,
                    requirement_key, document_type_key, process_area,
                    requirement_level, requirement_status, condition_snapshot
                ) VALUES (
                    :tenant_id, :journey_id, NULL,
                    'booking_bank_statement', 'bank_statement_extract', 'BOOKING',
                    'OPTIONAL', 'PENDING', '{}'::jsonb
                )
                ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING
                """
            ),
            {
                "tenant_id": audit_setup["tenant_id"],
                "journey_id": audit_setup["journey_id"],
            },
        )

    summary = _client().get(f"{_base(audit_setup)}/audit-summary")
    assert summary.status_code == 200
    completed = _client().post(
        f"{_base(audit_setup)}/stages/BOOKING/audit/complete",
        headers={
            "Idempotency-Key": "booking-audit-non-checklist",
            "If-Match": f'"{summary.json()["booking"]["aggregateVersion"]}"',
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
    # v1.1 correction: PC raises nothing, new or existing -- every
    # observation is TL/PM's to record, or the machine's. A self-serve gap
    # normally closes itself via PC's own auto-spawned Task instead.
    assert "RAISE" not in pc.json()["permittedActions"]
    assert "RESOLVE" not in pc.json()["permittedActions"]
    assert "REMARK" not in pc.json()["permittedActions"]
    assert "CONFIRM_BREACH" not in pc.json()["permittedActions"]
    assert "MARK_FALSE_POSITIVE" not in pc.json()["permittedActions"]
    assert "ACKNOWLEDGE" not in pc.json()["permittedActions"]

    _set_role(audit_setup, "TL")
    tl = _client().get(f"{_base(audit_setup)}/audit-summary")
    assert tl.status_code == 200
    assert "RESOLVE" in tl.json()["permittedActions"]
    assert "CONFIRM_BREACH" in tl.json()["permittedActions"]
    assert "TAKE_ACTION" in tl.json()["permittedActions"]
    assert "ESCALATE" in tl.json()["permittedActions"]
    assert "VOID" not in tl.json()["permittedActions"]

    _set_role(audit_setup, "EXECUTIVE")
    executive = _client().get(f"{_base(audit_setup)}/audit-summary")
    assert executive.status_code == 200
    assert "VOID" in executive.json()["permittedActions"]
