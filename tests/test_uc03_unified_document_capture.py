from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_delivery_capture_v2 import _delivery_requirements
from audit_core.uc03_unified_document_capture import (
    _correct_durable_store_stage,
    _receipt_defaults_to_delivery,
    _requirements_owned_by_stage,
    reconcile_unified_documents,
    resolve_document_stage,
)


def _requirement(key: str, document_type: str, requirement_key: str | None = None) -> dict:
    return {
        "document_type_key": document_type,
        "requirement_key": requirement_key or key,
    }


BOOKING_REQS = [_requirement("booking_docket", "booking_form"), _requirement("pan_card", "pan_card")]
DELIVERY_REQS = [_requirement("delivery_ndc", "NO_DUES_CERTIFICATE"), _requirement("delivery_car_pictures", "CAR_PICTURES")]


# Stage is now a static property of the type itself (_BOOKING_ONLY_
# DOCUMENT_TYPES in uc03_unified_document_capture.py), not resolved by
# matching against whichever requirement rows a journey happens to have --
# these calls never touch the database for a non-receipt type, so a
# placeholder connection/tenant/journey is fine.
_UNUSED_CONNECTION = None
_UNUSED_TENANT = "unused-tenant"
_UNUSED_JOURNEY = uuid4()


def test_resolve_document_stage_matches_delivery_type() -> None:
    # NO_DUES_CERTIFICATE isn't in the fixed Booking list, so it defaults
    # to Delivery regardless of what either requirement list contains.
    stage, requirement_key = resolve_document_stage(
        _UNUSED_CONNECTION, "NO_DUES_CERTIFICATE", tenant_id=_UNUSED_TENANT, journey_id=_UNUSED_JOURNEY,
        booking_requirements=BOOKING_REQS, delivery_requirements=DELIVERY_REQS,
    )
    assert stage == "DELIVERY"
    assert requirement_key == "delivery_ndc"


def test_resolve_document_stage_matches_booking_type() -> None:
    stage, requirement_key = resolve_document_stage(
        _UNUSED_CONNECTION, "pan_card", tenant_id=_UNUSED_TENANT, journey_id=_UNUSED_JOURNEY,
        booking_requirements=BOOKING_REQS, delivery_requirements=DELIVERY_REQS,
    )
    assert stage == "BOOKING"
    assert requirement_key == "pan_card"


def test_resolve_document_stage_unlisted_type_defaults_to_delivery_even_with_a_booking_row() -> None:
    """Direct user correction (2026-09-23): stage is decided by the fixed
    Booking list alone, never by which journey_document_requirements rows
    happen to exist -- a type that isn't in that list defaults to Delivery
    even if some tenant's requirement catalog happens to register it under
    BOOKING (the exact per-journey-data-dependent ambiguity this replaces)."""
    stage, _ = resolve_document_stage(
        _UNUSED_CONNECTION, "gate_pass", tenant_id=_UNUSED_TENANT, journey_id=_UNUSED_JOURNEY,
        booking_requirements=[_requirement("some_booking_gate_pass_row", "gate_pass")],
        delivery_requirements=[],
    )
    assert stage == "DELIVERY"


def test_resolve_document_stage_defaults_a_classified_but_unlisted_type_to_delivery() -> None:
    # "all other docs are under delivery" -- a real, classified type that
    # isn't in the fixed Booking list defaults to Delivery, not Booking.
    stage, requirement_key = resolve_document_stage(
        _UNUSED_CONNECTION, "totally_unknown_type", tenant_id=_UNUSED_TENANT, journey_id=_UNUSED_JOURNEY,
        booking_requirements=BOOKING_REQS, delivery_requirements=DELIVERY_REQS,
    )
    assert stage == "DELIVERY"
    assert requirement_key is None


def test_resolve_document_stage_handles_missing_classification() -> None:
    # No classification at all yet (still mid-classification) is a
    # genuinely different case from "classified as something we don't list"
    # -- defaults to BOOKING as a safe placeholder, the stage that always
    # exists, until real classification arrives.
    stage, requirement_key = resolve_document_stage(
        _UNUSED_CONNECTION, None, tenant_id=_UNUSED_TENANT, journey_id=_UNUSED_JOURNEY,
        booking_requirements=BOOKING_REQS, delivery_requirements=DELIVERY_REQS,
    )
    assert stage == "BOOKING"
    assert requirement_key is None


def test_requirements_owned_by_stage_drops_a_row_the_catalog_mislabels() -> None:
    """Direct bug this closes (2026-09-23): create_unified_upload_intents
    used to bind a requirement_ref from whichever stage's catalog happened
    to register a type first -- the same per-catalog dependency
    resolve_document_stage was fixed to not use. A Booking-catalog row for
    a type that isn't in the fixed Booking list (see
    test_resolve_document_stage_unlisted_type_defaults_to_delivery_even_
    with_a_booking_row) must never be returned as BOOKING-owned."""
    mislabeled = [_requirement("some_booking_gate_pass_row", "gate_pass")]
    # A tenant's requirement catalog registered this gate_pass row under
    # BOOKING -- but gate_pass isn't in the fixed Booking list, so filtering
    # the BOOKING-sourced list against "BOOKING" drops it: it must never
    # supply a Booking requirement_ref regardless of which catalog it's in.
    assert _requirements_owned_by_stage(
        mislabeled, "BOOKING", receipt_defaults_to_delivery=False,
    ) == []
    # The same row filtered against its type's real, correct stage is kept
    # -- proving it would bind correctly if it existed in Delivery's own
    # catalog list instead (the function filters by type, not by which
    # list it's called with).
    assert _requirements_owned_by_stage(
        mislabeled, "DELIVERY", receipt_defaults_to_delivery=False,
    ) == mislabeled


def test_requirements_owned_by_stage_keeps_a_correctly_labeled_row() -> None:
    assert _requirements_owned_by_stage(
        BOOKING_REQS, "BOOKING", receipt_defaults_to_delivery=False,
    ) == BOOKING_REQS
    assert _requirements_owned_by_stage(
        DELIVERY_REQS, "DELIVERY", receipt_defaults_to_delivery=False,
    ) == DELIVERY_REQS


def test_requirements_owned_by_stage_follows_the_receipt_running_total() -> None:
    receipt_row = [_requirement("booking_payment_receipt", "dealer_receipt")]
    assert _requirements_owned_by_stage(
        receipt_row, "BOOKING", receipt_defaults_to_delivery=False,
    ) == receipt_row
    assert _requirements_owned_by_stage(
        receipt_row, "DELIVERY", receipt_defaults_to_delivery=False,
    ) == []
    assert _requirements_owned_by_stage(
        receipt_row, "DELIVERY", receipt_defaults_to_delivery=True,
    ) == receipt_row
    assert _requirements_owned_by_stage(
        receipt_row, "BOOKING", receipt_defaults_to_delivery=True,
    ) == []


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

        # Reported live: the Booking screen's own "live" read (unrelated to
        # this module) re-runs its own reconciliation against every
        # DI document it sees, using a Booking-only requirement lookup --
        # before it was scoped to stage_code='BOOKING' (matching
        # uc03_delivery_capture_v2._reconcile_delivery_documents' own,
        # already-correct scoping), it would find this Delivery-routed
        # document (still visible in DI's BOOKING-phase list -- this module
        # always uses that one phase, see the module docstring), fail to
        # match NO_DUES_CERTIFICATE against Booking's own requirements, and
        # null the requirement_key this reconcile just correctly set.
        from audit_core.uc03_document_capture_v2 import (
            _base_requirements,
            _reconcile_documents,
        )

        booking_requirements = _base_requirements(connection, setup["tenant_id"], setup["journey_id"])
        _reconcile_documents(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            requirements=booking_requirements,
            di_documents=[{
                "documentId": str(setup["document_id"]),
                "state": "CLASSIFIED",
                "classifiedDocumentTypeKey": "NO_DUES_CERTIFICATE",
            }],
        )
        row_after_booking_poll = connection.execute(
            text("""
                SELECT stage_code, requirement_key
                FROM auditcore.document_capture_v2_documents
                WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:doc
                """),
            {"t": setup["tenant_id"], "j": setup["journey_id"], "doc": setup["document_id"]},
        ).mappings().one()
        assert row_after_booking_poll["stage_code"] == "DELIVERY"
        assert row_after_booking_poll["requirement_key"] == "NDC"


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


def test_requirements_with_open_slot_excludes_a_fulfilled_single_document_requirement(
    unified_capture_setup,
) -> None:
    """Confirmed live (2026-09-23): a second upload of a single-document
    requirement type (e.g. a duplicate booking_form) still got handed the
    same requirement_ref DI already had for the first one, so it extracted
    -- and then permanently failed to link (a requirement can only have one
    active evidence link), wasting DI compute on data nothing would ever
    read. _requirements_with_open_slot is what lets Audit Core simply not
    hand out a ref for an already-fulfilled single-document requirement in
    the first place -- DI still classifies the duplicate (candidate_
    document_type_keys is built from the full, unfiltered list separately),
    it just never gets queued for extraction.
    """
    from audit_core.uc03_document_capture_v2 import (
        _base_requirements,
        _requirements_with_open_slot,
    )

    setup = unified_capture_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        requirements = _base_requirements(connection, setup["tenant_id"], setup["journey_id"])
        booking_docket = next(r for r in requirements if r["requirement_key"] == "BOOKING_DOCKET")

        # No evidence yet -- BOOKING_DOCKET has an open slot.
        open_before = _requirements_with_open_slot(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
            requirements=requirements,
        )
        assert "BOOKING_DOCKET" in {r["requirement_key"] for r in open_before}

        connection.execute(
            text("""
                INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id, journey_document_requirement_id,
                    di_subject_id, di_document_id, evidence_purpose
                ) VALUES (
                    :t, :j, :cu, :req, :subj, :doc, 'DOCUMENT_CAPTURE'
                )
                """),
            {
                "t": setup["tenant_id"], "j": setup["journey_id"],
                "cu": connection.execute(
                    text("SELECT customer_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
                    {"t": setup["tenant_id"], "j": setup["journey_id"]},
                ).scalar_one(),
                "req": booking_docket["requirement_ref"],
                "subj": uuid4(), "doc": uuid4(),
            },
        )

        open_after = _requirements_with_open_slot(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
            requirements=requirements,
        )
        assert "BOOKING_DOCKET" not in {r["requirement_key"] for r in open_after}
        # Nothing else in the requirement set was touched by this one
        # requirement becoming fulfilled.
        assert len(open_after) == len(open_before) - 1


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


def _seed_extracted_field(
    connection,
    *,
    tenant_id: str,
    journey_id,
    di_document_id,
    actor_id: str,
    stage_code: str,
    source_canonical_field_id: str,
    document_type_key: str = "no_dues_certificate",
    field_key: str = "ndc_reference",
    value: str = "NDC-12345",
) -> None:
    customer_id = connection.execute(
        text("SELECT customer_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    # evidence has its own UNIQUE (tenant_id, di_document_id) -- seeding two
    # extracted-field rows for the SAME di_document_id under two different
    # stages (this module's own collision-guard test does exactly that)
    # can't share one evidence row's di_document_id either, so each
    # evidence row here gets its own synthetic one. Nothing enforces
    # evidence.di_document_id == journey_document_extracted_fields.
    # di_document_id at the DB level; only the latter is what
    # _documents_from_durable_store/_correct_durable_store_stage key on.
    evidence_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.evidence (
                tenant_id, journey_id, customer_id, di_subject_id, di_document_id,
                document_type_key, evidence_purpose, linked_by_actor_id
            ) VALUES (
                :t, :j, :cu, :subject, :evidence_doc, :dtype, 'DELIVERY_AUDIT', :actor
            ) RETURNING evidence_id
            """
        ),
        {
            "t": tenant_id, "j": journey_id, "cu": customer_id, "subject": uuid4(), "evidence_doc": uuid4(),
            "dtype": document_type_key, "actor": actor_id,
        },
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_extracted_fields (
                tenant_id, journey_id, evidence_id, di_document_id,
                stage_code, source_document_type_key, source_canonical_field_id,
                field_key, effective_value, source_fact_version
            ) VALUES (
                :t, :j, :ev, :doc,
                :stage, :dtype, :canonical_field_id,
                :field_key, CAST(:val AS jsonb), 1
            )
            """
        ),
        {
            "t": tenant_id, "j": journey_id, "ev": evidence_id, "doc": di_document_id,
            "stage": stage_code, "dtype": document_type_key, "canonical_field_id": source_canonical_field_id,
            "field_key": field_key, "val": json.dumps(value),
        },
    )


def test_reconcile_unified_documents_corrects_a_stale_durable_store_stage_and_rematerializes(
    unified_capture_setup, monkeypatch,
) -> None:
    """Regression: document_capture_v2_documents.stage_code (the checklist's
    own column) already self-heals on every reconcile, but journey_document_
    extracted_fields.stage_code -- the column the actual Delivery/Booking
    materializers key their durable-store read on -- never did. A document
    whose facts were durably written under a stale stage guess (BOOKING,
    resolve_document_stage's own default) stayed permanently invisible to
    insurance/registration/finance/commercial-lines materialization even
    once correctly classified, no matter how many times Resync ran, since
    Resync's own re-sync reads this exact same never-corrected column.
    Confirmed live: a document whose checklist entry correctly said
    "Delivery" left every one of those canonical tables empty."""
    setup = unified_capture_setup
    di_document_id = setup["document_id"]

    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_extracted_field(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            di_document_id=di_document_id,
            actor_id=setup["actor_id"],
            stage_code="BOOKING",
            source_canonical_field_id="ndc-field-1",
        )

    rematerialize_calls: list[tuple[str, object]] = []

    def _spy(connection, *, tenant_id, journey_id):
        rematerialize_calls.append((tenant_id, journey_id))
        return {"spied": True}

    monkeypatch.setattr(
        "audit_core.uc03_delivery_post_extraction_materialization.materialize_delivery_documents_from_durable_store",
        _spy,
    )

    v2_client = _FakeV2Client(documents_by_phase={
        "BOOKING": [
            {
                "documentId": str(di_document_id),
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
            correlation_id="test-reconcile-stage-fix",
            v2_client=v2_client,
            context_ref="ctx",
            token="tok",
        )

        stage_after = connection.execute(
            text(
                """
                SELECT stage_code FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:doc
                  AND source_canonical_field_id='ndc-field-1'
                """
            ),
            {"t": setup["tenant_id"], "j": setup["journey_id"], "doc": di_document_id},
        ).scalar_one()
        assert stage_after == "DELIVERY"

    assert rematerialize_calls == [(setup["tenant_id"], setup["journey_id"])]


def test_correct_durable_store_stage_never_collides_with_an_already_correct_row(
    unified_capture_setup,
) -> None:
    """A later, correctly-tagged webhook redelivery can independently have
    already written the right (stage_code=DELIVERY) row for the very same
    document/field/version identity before reconcile ever runs. Blindly
    UPDATEing the stale BOOKING row into that same identity would violate
    journey_document_extracted_fields' own partial unique index
    (tenant_id, journey_id, stage_code, di_document_id,
    source_canonical_field_id, source_fact_version) -- the fix must detect
    that and leave the stale row alone instead of raising."""
    setup = unified_capture_setup
    di_document_id = setup["document_id"]

    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_extracted_field(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            di_document_id=di_document_id,
            actor_id=setup["actor_id"],
            stage_code="BOOKING",
            source_canonical_field_id="ndc-field-2",
            value="stale-booking-copy",
        )
        _seed_extracted_field(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            di_document_id=di_document_id,
            actor_id=setup["actor_id"],
            stage_code="DELIVERY",
            source_canonical_field_id="ndc-field-2",
            value="already-correct-copy",
        )

        corrected = _correct_durable_store_stage(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            document_id=di_document_id,
            stage_code="DELIVERY",
        )
        assert corrected is False

        rows = connection.execute(
            text(
                """
                SELECT stage_code, effective_value FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:doc
                  AND source_canonical_field_id='ndc-field-2'
                ORDER BY stage_code
                """
            ),
            {"t": setup["tenant_id"], "j": setup["journey_id"], "doc": di_document_id},
        ).mappings().all()
        assert [r["stage_code"] for r in rows] == ["BOOKING", "DELIVERY"]
        assert rows[0]["effective_value"] == "stale-booking-copy"
        assert rows[1]["effective_value"] == "already-correct-copy"


def test_receipt_defaults_to_booking_below_minimum_amount(unified_capture_setup) -> None:
    """No tenant_rule_config row in this fixture -> the ₹11,000 default
    (_DEFAULT_MINIMUM_BOOKING_AMOUNT) applies. A single already-extracted
    receipt for ₹5,000 hasn't reached it yet, so the NEXT receipt still
    defaults to Booking."""
    setup = unified_capture_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_extracted_field(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            di_document_id=uuid4(),
            actor_id=setup["actor_id"],
            stage_code="BOOKING",
            source_canonical_field_id="receipt-amount-1",
            document_type_key="dealer_receipt",
            field_key="amount_paid",
            value="5000",
        )

        assert _receipt_defaults_to_delivery(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
        ) is False

        stage, _ = resolve_document_stage(
            connection, "dealer_receipt", tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
            booking_requirements=[], delivery_requirements=[],
        )
        assert stage == "BOOKING"


def test_receipt_defaults_to_delivery_once_running_total_reaches_minimum(unified_capture_setup) -> None:
    """Two already-extracted receipts (₹6,000 + ₹6,000 = ₹12,000) push the
    running total at/above the ₹11,000 default minimum -- a NEW receipt now
    defaults to Delivery. Direct user directive (2026-09-23): 'once minimum
    booking amount is received, all other payment receipts can be
    considered for delivery.'"""
    setup = unified_capture_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_extracted_field(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            di_document_id=uuid4(),
            actor_id=setup["actor_id"],
            stage_code="BOOKING",
            source_canonical_field_id="receipt-amount-1",
            document_type_key="dealer_receipt",
            field_key="amount_paid",
            value="6000",
        )
        _seed_extracted_field(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            di_document_id=uuid4(),
            actor_id=setup["actor_id"],
            stage_code="BOOKING",
            source_canonical_field_id="receipt-amount-2",
            document_type_key="dealer_receipt",
            field_key="amount_paid",
            value="6000",
        )

        assert _receipt_defaults_to_delivery(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
        ) is True

        stage, _ = resolve_document_stage(
            connection, "dealer_receipt", tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
            booking_requirements=[], delivery_requirements=[],
        )
        assert stage == "DELIVERY"


def test_receipt_running_total_canonicalizes_payment_receipt_with_dealer_receipt(
    unified_capture_setup,
) -> None:
    """The running total must count a payment_receipt-typed row (Delivery's
    own historical type name for the same physical document) toward the
    same total as dealer_receipt -- they're one canonical identity
    (uc03_document_capture_v2._DOCUMENT_TYPE_ALIASES), not two."""
    setup = unified_capture_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_extracted_field(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            di_document_id=uuid4(),
            actor_id=setup["actor_id"],
            stage_code="BOOKING",
            source_canonical_field_id="receipt-amount-1",
            document_type_key="dealer_receipt",
            field_key="amount_paid",
            value="6000",
        )
        _seed_extracted_field(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            di_document_id=uuid4(),
            actor_id=setup["actor_id"],
            stage_code="DELIVERY",
            source_canonical_field_id="receipt-amount-2",
            document_type_key="payment_receipt",
            field_key="amount_paid",
            value="6000",
        )

        assert _receipt_defaults_to_delivery(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
        ) is True
