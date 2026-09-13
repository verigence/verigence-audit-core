from __future__ import annotations

import inspect
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
from audit_core.uc03_audit_flags import act_on_flag
from audit_core.uc03_document_field_corrections import (
    apply_confirmed_field_correction,
    submit_field_correction,
)


@dataclass
class AllowedAuthorization:
    def check_user_permission(self, *, user_id: str, tenant_id: str, permission_key: str):
        return SecurityAuthorizationDecision(
            allowed=True,
            reason_code="AUTHORIZED",
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key=None,
        )


@pytest.fixture
def correction_setup():
    """Self-contained -- deliberately not imported from another test module
    (see test_uc03_journey_overview_sku_panel.py's ``journey`` fixture for
    why: a plain `pytest` invocation, as CI runs it, doesn't add the repo
    root to sys.path, so `from tests.<mod> import <fixture>` resolves
    locally but raises ModuleNotFoundError in real CI)."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 audit integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-uc03-corr-{suffix}"
    actors = {"PC": f"uc03-corr-pc-{suffix}", "TL": f"uc03-corr-tl-{suffix}"}
    document_id = uuid4()

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:c, 'V') RETURNING product_category_id"
            ),
            {"c": f"UC03-CORR-CAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:c, 'O') RETURNING oem_id"
            ),
            {"c": f"UC03-CORR-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :t, :pc, 'UC03 Corr Project', :o, :cat,
                    CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {"t": tenant_id, "pc": f"UC03-CORR-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:t, :c, 'D') RETURNING dealer_id"
            ),
            {"t": tenant_id, "c": f"CORR-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealer_outlets "
                "(tenant_id, dealer_id, outlet_code, outlet_name) "
                "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"
            ),
            {"t": tenant_id, "d": dealer_id, "c": f"CORR-O-{suffix}"},
        ).scalar_one()
        for role, actor_id in actors.items():
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.business_assignments (
                        tenant_id, security_actor_id, business_role_code,
                        dealer_id, outlet_id
                    ) VALUES (:t, :a, :r, :d, :o)
                    """
                ),
                {"t": tenant_id, "a": actor_id, "r": role, "d": dealer_id, "o": outlet_id},
            )
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (:t, :d, :o, 'INDIVIDUAL', 'Corr Customer') RETURNING customer_id
                """
            ),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id, journey_reference
                ) VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id
                """
            ),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"CORR-J-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_stage_states (
                    tenant_id, journey_id, stage_code, business_status,
                    audit_state, audit_status, first_started_at_utc,
                    latest_activity_at_utc, version_no
                ) VALUES (
                    :t, :j, 'BOOKING', 'BOOKING_IN_PROGRESS',
                    'IN_PROGRESS', 'NOT_EVALUATED', now(), now(), 1
                )
                """
            ),
            {"t": tenant_id, "j": journey_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.document_capture_v2_documents (
                    tenant_id, journey_id, stage_code, di_document_id,
                    client_upload_id, requirement_key,
                    classified_document_type_key, capture_status,
                    original_filename, created_by_actor_id
                ) VALUES (
                    :t, :j, 'BOOKING', :doc, :upload, 'booking_form',
                    'booking_form', 'CLASSIFIED', 'booking_form.pdf', :actor
                )
                """
            ),
            {
                "t": tenant_id,
                "j": journey_id,
                "doc": document_id,
                "upload": f"upload-{suffix}",
                "actor": actors["PC"],
            },
        )

    active_actor = {"id": actors["PC"]}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=active_actor["id"])
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowedAuthorization()
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
            "actors": actors,
            "active_actor": active_actor,
        }
    finally:
        app.dependency_overrides.pop(get_human_principal, None)
        app.dependency_overrides.pop(get_security_authorization_client, None)
        engine.dispose()


def _client() -> TestClient:
    return TestClient(app)


def _set_role(setup, role: str) -> None:
    setup["active_actor"]["id"] = setup["actors"][role]


def _submit(
    setup,
    *,
    key: str,
    field_key: str = "chassis_number",
    new_value: str = "MA3ECORRECTED001",
    confidence: float | None = 96.0,
    remarks: str | None = "Chassis number was mis-read from a smudged booking form.",
    source_fact_version: int = 1,
):
    return _client().post(
        f"/v2/tenants/{setup['tenant_id']}/journeys/{setup['journey_id']}"
        f"/uc03/documents/{setup['document_id']}/field-corrections",
        headers={"Idempotency-Key": key},
        json={
            "stage": "BOOKING",
            "documentId": str(setup["document_id"]),
            "documentTypeKey": "booking_form",
            "fieldKey": field_key,
            "canonicalFieldId": "canon-chassis-1",
            "sourceFactVersion": source_fact_version,
            "confidenceScore": confidence,
            "originalValue": "MA3EWRONG00000",
            "newValue": new_value,
            "remarks": remarks,
        },
    )


# Kept as an alias so the >=90% (adjudicated) tests below read naturally as
# "propose" -- the endpoint is the same submit_field_correction either way,
# branching internally on confidence.
_propose = _submit


def test_propose_field_correction_raises_a_tl_owned_violation(correction_setup):
    response = _propose(correction_setup, key="propose-0001")
    assert response.status_code == 200, response.text
    flag = response.json()["flag"]
    assert flag["findingClass"] == "VIOLATION"
    assert flag["resolutionMode"] == "ADJUDICATED"
    assert flag["ownerRoleCode"] == "TL"
    assert flag["status"] == "OPEN"
    assert flag["ruleKey"] == "DI_VALUE_CORRECTION_PROPOSED:chassis_number"
    # Self-serve documents-complete criterion must never be blocked by this.
    assert flag["blockingCompletion"] is False
    # A PC may only comment on it, matching every other VIOLATION.
    assert flag["permittedActions"] == ["REMARK"]


def test_confirm_breach_applies_the_proposed_value(correction_setup):
    flag = _propose(correction_setup, key="propose-0002").json()["flag"]
    _set_role(correction_setup, "TL")
    action = _client().post(
        f"/v1/tenants/{correction_setup['tenant_id']}/journeys/{correction_setup['journey_id']}"
        f"/uc03/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "confirm-0002", "If-Match": f'"{flag["version"]}"'},
        json={"action": "CONFIRM_BREACH", "resolutionReason": "Verified against the chassis plate photo."},
    )
    assert action.status_code == 200, action.text
    resolved = action.json()["flag"]
    assert resolved["status"] == "RESOLVED"
    assert resolved["disposition"] == "CONFIRMED_BREACH"

    with correction_setup["engine"].connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT effective_value, is_modified
                FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id=:t AND journey_id=:j AND field_key='chassis_number'
                """
            ),
            {"t": correction_setup["tenant_id"], "j": correction_setup["journey_id"]},
        ).mappings().one()
    assert row["effective_value"] == "MA3ECORRECTED001"
    assert row["is_modified"] is True

    with correction_setup["engine"].connect() as connection:
        proposal = connection.execute(
            text(
                """
                SELECT applied_at_utc FROM auditcore.journey_document_field_correction_proposals
                WHERE tenant_id=:t AND audit_finding_id=:f
                """
            ),
            {"t": correction_setup["tenant_id"], "f": UUID(flag["flagId"])},
        ).scalar_one()
    assert proposal is not None


def test_mark_false_positive_leaves_the_original_value_untouched(correction_setup):
    flag = _propose(correction_setup, key="propose-0003").json()["flag"]
    _set_role(correction_setup, "TL")
    action = _client().post(
        f"/v1/tenants/{correction_setup['tenant_id']}/journeys/{correction_setup['journey_id']}"
        f"/uc03/flags/{flag['flagId']}/actions",
        headers={"Idempotency-Key": "confirm-0003", "If-Match": f'"{flag["version"]}"'},
        json={"action": "MARK_FALSE_POSITIVE", "resolutionReason": "The scanned value was actually correct."},
    )
    assert action.status_code == 200, action.text
    resolved = action.json()["flag"]
    assert resolved["status"] == "RESOLVED"
    assert resolved["disposition"] == "FALSE_POSITIVE"

    with correction_setup["engine"].connect() as connection:
        stored = connection.execute(
            text(
                """
                SELECT effective_value FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id=:t AND journey_id=:j AND field_key='chassis_number'
                """
            ),
            {"t": correction_setup["tenant_id"], "j": correction_setup["journey_id"]},
        ).scalar_one_or_none()
    assert stored is None  # never applied -- MARK_FALSE_POSITIVE writes nothing


def test_low_confidence_correction_applies_immediately_and_raises_an_info_flag(correction_setup):
    # <90% (self-serve): the value must already be visible in Audit Core
    # from THIS SAME response -- no separate Confirm/CONFIRM_BREACH step,
    # and no remarks required (a PC could always do this directly on the
    # old Review page; a mandatory remark here would just be new friction).
    response = _submit(
        correction_setup,
        key="submit-lowconf-0001",
        field_key="engine_number",
        new_value="EN-CORRECTED-1",
        confidence=72.0,
        remarks=None,
    )
    assert response.status_code == 200, response.text
    flag = response.json()["flag"]
    assert flag["findingClass"] == "DATA_GAP"
    assert flag["resolutionMode"] == "SELF_SERVICE"
    assert flag["ownerRoleCode"] == "PC"
    assert flag["severity"] == "INFO"
    assert flag["ruleKey"] == "DI_VALUE_CORRECTED:engine_number"

    with correction_setup["engine"].connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT effective_value, is_modified
                FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id=:t AND journey_id=:j AND field_key='engine_number'
                """
            ),
            {"t": correction_setup["tenant_id"], "j": correction_setup["journey_id"]},
        ).mappings().one()
    assert row["effective_value"] == "EN-CORRECTED-1"
    assert row["is_modified"] is True

    with correction_setup["engine"].connect() as connection:
        applied_at = connection.execute(
            text(
                """
                SELECT applied_at_utc FROM auditcore.journey_document_field_correction_proposals
                WHERE tenant_id=:t AND audit_finding_id=:f
                """
            ),
            {"t": correction_setup["tenant_id"], "f": UUID(flag["flagId"])},
        ).scalar_one()
    assert applied_at is not None  # stamped at creation, not deferred to a later action


def test_missing_confidence_is_treated_as_low_confidence(correction_setup):
    # requires_pc_review(None) is True -- an unscored field gets the same
    # self-serve, immediate-apply treatment as an explicit <90% score.
    response = _submit(
        correction_setup,
        key="submit-noconf-0001",
        field_key="unscored_field",
        new_value="whatever",
        confidence=None,
        remarks=None,
    )
    assert response.status_code == 200, response.text
    assert response.json()["flag"]["findingClass"] == "DATA_GAP"


def test_high_confidence_correction_requires_remarks(correction_setup):
    response = _submit(
        correction_setup,
        key="submit-noremark-0001",
        confidence=95.0,
        remarks=None,
    )
    # This app's error handler translates a pydantic validation failure to
    # 400/VAC-VAL-001, not FastAPI's default 422 -- matching every other
    # request-validation error in this codebase.
    assert response.status_code == 400, response.text
    assert response.json()["errorCode"] == "VAC-VAL-001"


def test_high_confidence_correction_does_not_apply_until_confirmed(correction_setup):
    # The >=90% path's whole point: nothing is written to Audit Core at
    # submit time, unlike the <90% path above.
    response = _submit(correction_setup, key="submit-highconf-noapply-0001", field_key="vin_pending")
    assert response.status_code == 200, response.text
    with correction_setup["engine"].connect() as connection:
        stored = connection.execute(
            text(
                """
                SELECT effective_value FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id=:t AND journey_id=:j AND field_key='vin_pending'
                """
            ),
            {"t": correction_setup["tenant_id"], "j": correction_setup["journey_id"]},
        ).scalar_one_or_none()
    assert stored is None


def test_submit_field_correction_never_touches_the_stage_wide_confirm_gate() -> None:
    # Regression guard for the exact bug this dual-path design fixes: the
    # stage-wide Confirm endpoints (pc_verification_status PENDING ->
    # VERIFIED, one-shot) must never be invoked from here -- doing so would
    # silently "verify" the whole stage off one field correction and
    # permanently lock out every later correction on any other document.
    source = inspect.getsource(submit_field_correction)
    assert "pc_verification_status" not in source
    assert "confirm_booking_review_v2" not in source
    assert "confirm_delivery_review_v2" not in source


def test_apply_hook_does_not_invoke_the_heavy_materialization_pass() -> None:
    # apply_confirmed_field_correction delegates to the shared
    # _apply_field_value helper (also used by the <90% immediate-apply
    # path), which reuses only persist_reviewed_di_fields + the conditional
    # typed-attribute projection, not materialize_reviewed_di_business_
    # values -- see the module docstring for why. Source-inspected so a
    # future edit that accidentally wires in the heavy pass fails loudly.
    from audit_core.uc03_document_field_corrections import _apply_field_value

    hook_source = inspect.getsource(apply_confirmed_field_correction)
    helper_source = inspect.getsource(_apply_field_value)
    assert "_apply_field_value(" in hook_source
    assert "materialize_reviewed_di_business_values" not in hook_source
    assert "materialize_reviewed_di_business_values" not in helper_source
    assert "persist_reviewed_di_fields(" in helper_source


def test_act_on_flag_only_applies_the_correction_on_confirm_breach() -> None:
    source = inspect.getsource(act_on_flag)
    assert "apply_confirmed_field_correction" in source
    assert 'payload.action == "CONFIRM_BREACH"' in source
    assert "DI_VALUE_CORRECTION_PROPOSED" in source
