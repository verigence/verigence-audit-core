from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_delivery_capture_v2 import _delivery_requirements
from audit_core.uc03_unified_document_capture import (
    reconcile_unified_documents,
    resolve_document_stage,
)


def _requirement(key: str, document_type: str, requirement_key: str | None = None) -> dict:
    return {
        "document_type_key": document_type,
        "requirement_key": requirement_key or key,
    }


BOOKING_REQS = [_requirement("booking_docket", "booking_form"), _requirement("pan_card", "pan")]
DELIVERY_REQS = [_requirement("delivery_ndc", "NO_DUES_CERTIFICATE"), _requirement("delivery_car_pictures", "CAR_PICTURES")]


def test_resolve_document_stage_matches_delivery_type() -> None:
    stage, requirement_key = resolve_document_stage(
        "NO_DUES_CERTIFICATE", booking_requirements=BOOKING_REQS, delivery_requirements=DELIVERY_REQS,
    )
    assert stage == "DELIVERY"
    assert requirement_key == "delivery_ndc"


def test_resolve_document_stage_matches_booking_type() -> None:
    stage, requirement_key = resolve_document_stage(
        "pan", booking_requirements=BOOKING_REQS, delivery_requirements=DELIVERY_REQS,
    )
    assert stage == "BOOKING"
    assert requirement_key == "pan_card"


def test_resolve_document_stage_defaults_unrecognized_to_booking() -> None:
    stage, requirement_key = resolve_document_stage(
        "totally_unknown_type", booking_requirements=BOOKING_REQS, delivery_requirements=DELIVERY_REQS,
    )
    assert stage == "BOOKING"
    assert requirement_key is None


def test_resolve_document_stage_handles_missing_classification() -> None:
    stage, requirement_key = resolve_document_stage(
        None, booking_requirements=BOOKING_REQS, delivery_requirements=DELIVERY_REQS,
    )
    assert stage == "BOOKING"
    assert requirement_key is None


@dataclass
class _FakeV2Client:
    """Duck-typed stand-in for DiCaptureV2Client.list_documents -- reconcile_
    unified_documents only ever calls that one method, keyed by phase."""

    documents_by_phase: dict[str, list[dict]] = field(default_factory=dict)

    def list_documents(self, *, token, tenant_id, external_context_ref, phase):
        return {"documents": self.documents_by_phase.get(phase, [])}


@pytest.fixture
def unified_capture_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 unified capture integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-uc03-uni-{suffix}"
    actor_id = f"uc03-uni-pc-{suffix}"
    document_id = uuid4()

    with engine.begin() as connection:
        category_id = connection.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"UNI-CAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"UNI-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id, "
                 "product_category_id, effective_start_date, timezone_name, project_status) "
                 "VALUES (:t, :c, 'Unified Capture Project', :o, :cat, CURRENT_DATE - 1, "
                 "'Asia/Kolkata', 'ACTIVE')"),
            {"t": tenant_id, "c": f"UNI-{suffix}", "o": oem_id, "cat": category_id},
        )
        profile_id = connection.execute(
            text("INSERT INTO auditcore.document_requirement_profiles (tenant_id, profile_code, profile_name) "
                 "VALUES (:t, :c, 'Unified Capture') RETURNING document_requirement_profile_id"),
            {"t": tenant_id, "c": f"UNI-PROFILE-{suffix}"},
        ).scalar_one()
        profile_version_id = connection.execute(
            text("INSERT INTO auditcore.document_requirement_profile_versions "
                 "(tenant_id, document_requirement_profile_id, version_no, lifecycle_status, effective_from) "
                 "VALUES (:t, :p, 1, 'DRAFT', CURRENT_DATE - 1) "
                 "RETURNING document_requirement_profile_version_id"),
            {"t": tenant_id, "p": profile_id},
        ).scalar_one()
        connection.execute(
            text("""
                INSERT INTO auditcore.document_requirement_items (
                    tenant_id, document_requirement_profile_version_id,
                    requirement_key, document_type_key, process_area,
                    requirement_level, condition_config, sort_order
                ) VALUES
                (:t, :p, 'BOOKING_DOCKET', 'booking_form', 'BOOKING', 'REQUIRED', '{}'::jsonb, 10),
                (:t, :p, 'NDC', 'NO_DUES_CERTIFICATE', 'DELIVERY', 'REQUIRED', '{}'::jsonb, 20)
                """),
            {"t": tenant_id, "p": profile_version_id},
        )
        connection.execute(
            text("UPDATE auditcore.document_requirement_profile_versions "
                 "SET lifecycle_status='PUBLISHED' "
                 "WHERE tenant_id=:t AND document_requirement_profile_version_id=:p"),
            {"t": tenant_id, "p": profile_version_id},
        )
        dealer_id = connection.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"UNI-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"UNI-O-{suffix}"},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, "
                 "business_role_code, dealer_id, outlet_id) VALUES (:t, :a, 'PC', :d, :o)"),
            {"t": tenant_id, "a": actor_id, "d": dealer_id, "o": outlet_id},
        )
        customer_id = connection.execute(
            text("INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, customer_type_code, "
                 "display_name) VALUES (:t, :d, :o, 'INDIVIDUAL', 'Unified Customer') RETURNING customer_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, "
                 "journey_reference, document_requirement_profile_version_id) "
                 "VALUES (:t, :d, :o, :cu, :r, :pv) RETURNING journey_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id,
             "r": f"UNI-J-{suffix}", "pv": profile_version_id},
        ).scalar_one()
        connection.execute(
            text("""
                INSERT INTO auditcore.journey_stage_states (
                    tenant_id, journey_id, stage_code, business_status,
                    audit_state, audit_status, first_started_at_utc,
                    latest_activity_at_utc, version_no
                ) VALUES (
                    :t, :j, 'BOOKING', 'BOOKING_CLOSED',
                    'IN_PROGRESS', 'NOT_EVALUATED', now(), now(), 1
                )
                """),
            {"t": tenant_id, "j": journey_id},
        )
        # BOOKING_CLOSED alone (no closure_disposition) matches the
        # "Delivery sequence conflict" guard's ELSE branch -- set it
        # explicitly to PROCEED_TO_DELIVERY so ensure_delivery_started can
        # actually succeed in this fixture.
        connection.execute(
            text("UPDATE auditcore.journey_stage_states SET closure_disposition='PROCEED_TO_DELIVERY' "
                 "WHERE tenant_id=:t AND journey_id=:j AND stage_code='BOOKING'"),
            {"t": tenant_id, "j": journey_id},
        )
        connection.execute(
            text("""
                INSERT INTO auditcore.document_capture_v2_documents (
                    tenant_id, journey_id, stage_code, di_document_id,
                    client_upload_id, requirement_key, classified_document_type_key,
                    capture_status, original_filename, created_by_actor_id
                ) VALUES (
                    :t, :j, 'BOOKING', :doc, :upload, NULL, NULL,
                    'CLASSIFYING', 'ndc.pdf', :actor
                )
                """),
            {"t": tenant_id, "j": journey_id, "doc": document_id, "upload": f"upload-{suffix}", "actor": actor_id},
        )

    yield {
        "engine": engine,
        "tenant_id": tenant_id,
        "journey_id": journey_id,
        "document_id": document_id,
        "actor_id": actor_id,
    }
    engine.dispose()


def test_reconcile_unified_documents_dispatches_delivery_type_and_autostarts(unified_capture_setup) -> None:
    setup = unified_capture_setup
    v2_client = _FakeV2Client(documents_by_phase={
        "BOOKING": [
            {
                "documentId": str(setup["document_id"]),
                "state": "CLASSIFIED",
                "classifiedDocumentTypeKey": "NO_DUES_CERTIFICATE",
            }
        ],
    })
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        reconcile_unified_documents(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            actor_id=setup["actor_id"],
            actor_role="PC",
            correlation_id="test-reconcile-0001",
            v2_client=v2_client,
            context_ref="ctx",
            token="tok",
        )

        row = connection.execute(
            text("""
                SELECT stage_code, requirement_key, capture_status
                FROM auditcore.document_capture_v2_documents
                WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:doc
                """),
            {"t": setup["tenant_id"], "j": setup["journey_id"], "doc": setup["document_id"]},
        ).mappings().one()
        assert row["stage_code"] == "DELIVERY"
        assert row["requirement_key"] == "NDC"
        assert row["capture_status"] == "CLASSIFIED"

        delivery_started = connection.execute(
            text("SELECT business_status FROM auditcore.journey_stage_states "
                 "WHERE tenant_id=:t AND journey_id=:j AND stage_code='DELIVERY'"),
            {"t": setup["tenant_id"], "j": setup["journey_id"]},
        ).scalar_one()
        assert delivery_started == "DELIVERY_STARTED"


def test_reconcile_unified_documents_leaves_booking_type_alone(unified_capture_setup) -> None:
    setup = unified_capture_setup
    v2_client = _FakeV2Client(documents_by_phase={
        "BOOKING": [
            {
                "documentId": str(setup["document_id"]),
                "state": "CLASSIFIED",
                "classifiedDocumentTypeKey": "booking_form",
            }
        ],
    })
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        reconcile_unified_documents(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            actor_id=setup["actor_id"],
            actor_role="PC",
            correlation_id="test-reconcile-0002",
            v2_client=v2_client,
            context_ref="ctx",
            token="tok",
        )
        row = connection.execute(
            text("""
                SELECT stage_code, requirement_key
                FROM auditcore.document_capture_v2_documents
                WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:doc
                """),
            {"t": setup["tenant_id"], "j": setup["journey_id"], "doc": setup["document_id"]},
        ).mappings().one()
        assert row["stage_code"] == "BOOKING"
        assert row["requirement_key"] == "BOOKING_DOCKET"

        delivery_state = connection.execute(
            text("SELECT 1 FROM auditcore.journey_stage_states "
                 "WHERE tenant_id=:t AND journey_id=:j AND stage_code='DELIVERY'"),
            {"t": setup["tenant_id"], "j": setup["journey_id"]},
        ).scalar_one_or_none()
        assert delivery_state is None


def test_delivery_checklist_read_seeds_requirements_before_any_upload(unified_capture_setup) -> None:
    """Reported live: the combined Booking+Delivery checklist on Capture New
    Booking showed a Delivery section only after a document had been
    uploaded (upload-intents is what eagerly seeds Delivery's requirement
    rows). Opening the checklist on a brand-new Journey -- zero uploads --
    must show what Delivery will expect too, which means the read path
    itself (get_delivery_capture_local_v2) has to seed, not only the
    upload/reconcile paths. This proves the seed call the route makes."""
    setup = unified_capture_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])

        # Before anything is seeded: no DELIVERY journey_document_requirements
        # rows exist yet (nothing has ever uploaded a document or started
        # Delivery on this fresh Journey).
        before = _delivery_requirements(connection, setup["tenant_id"], setup["journey_id"])
        assert before == []

        # The exact call get_delivery_capture_local_v2 now makes before
        # building its response.
        connection.execute(
            text("SELECT auditcore.seed_delivery_document_requirements(:t, :j)"),
            {"t": setup["tenant_id"], "j": setup["journey_id"]},
        )

        # seed_delivery_document_requirements seeds this journey's whole
        # Delivery catalog (this fixture's own custom NDC item plus every
        # standing tenant-wide Delivery requirement -- wholesale invoice,
        # customer invoice, etc.), not just the one item this fixture added
        # -- the point of this test is that it went from nothing to
        # something, not the exact count.
        after = _delivery_requirements(connection, setup["tenant_id"], setup["journey_id"])
        assert len(after) > 0
        assert any(item["requirement_key"] == "NDC" for item in after)

        # Idempotent -- opening the checklist twice must not duplicate rows.
        connection.execute(
            text("SELECT auditcore.seed_delivery_document_requirements(:t, :j)"),
            {"t": setup["tenant_id"], "j": setup["journey_id"]},
        )
        again = _delivery_requirements(connection, setup["tenant_id"], setup["journey_id"])
        assert len(again) == len(after)
