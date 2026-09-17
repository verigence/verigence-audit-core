import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.uc03_booking_rule_trigger import (
    _booking_requirement_rule_specs,
    _requirement_satisfied,
    _requirement_snapshot,
)


def _row(
    key: str,
    *,
    level: str = "REQUIRED",
    status: str = "SATISFIED",
    answer: str = "YES",
    has_evidence: bool = False,
    document_type_key: str | None = None,
) -> dict[str, str | bool | None]:
    return {
        "requirement_key": key,
        "requirement_level": level,
        "requirement_status": status,
        "answer": answer,
        "has_evidence": has_evidence,
        "document_type_key": document_type_key,
    }


def test_identity_choice_suppresses_pan_flag_when_aadhaar_is_satisfied() -> None:
    specs = _booking_requirement_rule_specs(
        [
            _row("pan_card", status="PENDING", answer="UNANSWERED"),
            _row("aadhaar"),
        ]
    )

    assert "BK_PAN_PRESENT" not in {spec.rule_key for spec in specs}


def test_booking_checkpoint_maps_specific_missing_requirements_to_rules() -> None:
    specs = _booking_requirement_rule_specs(
        [
            _row("booking_docket", status="PENDING", answer="NO"),
            _row("pan_card", status="PENDING", answer="NO"),
            _row("aadhaar", status="PENDING", answer="NO"),
            _row("booking_payment_receipt", status="PENDING", answer="NO"),
        ]
    )

    assert {spec.rule_key for spec in specs} == {
        "BK_DOCKET_PRESENT",
        "BK_PAN_PRESENT",
        "BK_MIN_BOOKING_PROOF_PRESENT",
    }


def test_conditional_and_other_required_requirements_get_checkpoint_rules() -> None:
    specs = _booking_requirement_rule_specs(
        [
            _row("gst_certificate", level="CONDITIONAL", status="PENDING", answer="NO"),
            _row("some_future_required_doc", status="PENDING", answer="UNANSWERED"),
        ]
    )

    by_rule = {spec.rule_key: spec for spec in specs}
    assert by_rule["BK_CONDITIONAL_DOCS_ADDRESSED"].requirement_keys == ("gst_certificate",)
    assert by_rule["BK_REQUIRED_CAPTURE_COMPLETE"].requirement_keys == (
        "some_future_required_doc",
    )


def test_active_evidence_satisfies_a_requirement_even_with_no_explicit_answer() -> None:
    """Regression: journey_document_requirements.requirement_status is never
    actually transitioned to SATISFIED for Booking's V2 "upload everything"
    flow (only Delivery's own explicit per-document answer endpoint does
    that), and nothing in that flow ever answers a per-document applicability
    question either -- so requirement_status stayed PENDING and answer stayed
    UNANSWERED forever, regardless of how thoroughly a PC reviewed the actual
    linked evidence. Every requirement with real evidence linked used to be
    flagged outstanding unconditionally; it must not be once evidence exists.
    """
    assert _requirement_satisfied(
        _row("booking_docket", status="PENDING", answer="UNANSWERED", has_evidence=True)
    )

    specs = _booking_requirement_rule_specs(
        [
            _row("booking_docket", status="PENDING", answer="UNANSWERED", has_evidence=True),
            _row("pan_card", status="PENDING", answer="UNANSWERED", has_evidence=True),
            _row("booking_payment_receipt", status="PENDING", answer="UNANSWERED", has_evidence=True),
        ]
    )
    assert specs == []


def test_conditional_doc_already_covered_by_discount_evidence_is_not_double_flagged() -> None:
    """Regression: a live journey showed BOTH BK_DISCOUNT_EVIDENCE_MISSING:
    exchange_bonus and BK_CONDITIONAL_DOCS_ADDRESSED open at once, pointing
    the PC at two different tabs (Deal and Documents) for what turned out to
    be the exact same missing document -- trade_in_vehicle_rc's own
    document_type_key is 'vehicle_rc', precisely what
    uc03_booking_confirmation_rules already asks for when an exchange bonus
    is claimed. The generic conditional-docs rule must not re-flag a
    document type the more specific, business-named rule already covers.
    """
    specs = _booking_requirement_rule_specs(
        [
            _row(
                "trade_in_vehicle_rc", level="CONDITIONAL", status="PENDING",
                answer="NO", document_type_key="vehicle_rc",
            ),
        ]
    )
    assert specs == []

    # A conditional item NOT covered by a discount-evidence rule still flags
    # normally -- this isn't a blanket "never flag conditional docs" change.
    specs = _booking_requirement_rule_specs(
        [
            _row(
                "gst_certificate", level="CONDITIONAL", status="PENDING",
                answer="NO", document_type_key="gst_certificate",
            ),
        ]
    )
    by_rule = {spec.rule_key: spec for spec in specs}
    assert by_rule["BK_CONDITIONAL_DOCS_ADDRESSED"].requirement_keys == ("gst_certificate",)


def test_bk_min_booking_proof_present_fires_on_a_real_seeded_journey() -> None:
    """Regression: the pure-function tests above only prove the checkpoint
    logic is internally consistent -- they can't catch the check looking
    for a requirement_key no real journey ever carries. This drives the
    actual seeding trigger (trg_uc03_booking_initialize_requirements,
    inserted by starting Booking) and the real DB-backed snapshot reader,
    end to end, the way live traffic does. Before the fix this asserted
    False: real rows carry "booking_payment_receipt", the checkpoint
    checked "minimum_booking_payment_proof", so BK_MIN_BOOKING_PROOF_PRESENT
    could never fire no matter how incomplete a real Booking was."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-bkt-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"BKT-CAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"BKT-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id, "
                 "product_category_id, effective_start_date) "
                 "VALUES (:t, :pc, 'BKT', :o, :cat, CURRENT_DATE)"),
            {"t": tenant_id, "pc": f"BKT-{suffix}", "o": oem_id, "cat": category_id},
        )
        # The seeding trigger only inserts profile-driven rows for items
        # belonging to the journey's OWN document_requirement_profile_version_id
        # (the two or three unconditional inserts it also makes are all
        # OPTIONAL level, filtered out by _requirement_snapshot) -- a real
        # journey always has one of these, so the fixture needs one too.
        # A version can only publish with at least one Booking AND one
        # Delivery requirement (validate_document_profile_publish) -- the
        # Delivery item here is otherwise unused by this test.
        profile_id = connection.execute(
            text("INSERT INTO auditcore.document_requirement_profiles "
                 "(tenant_id, profile_code, profile_name) VALUES (:t, :c, 'BKT Profile') "
                 "RETURNING document_requirement_profile_id"),
            {"t": tenant_id, "c": f"BKT-PROFILE-{suffix}"},
        ).scalar_one()
        profile_version_id = connection.execute(
            text("INSERT INTO auditcore.document_requirement_profile_versions "
                 "(tenant_id, document_requirement_profile_id, version_no, lifecycle_status, "
                 "effective_from) VALUES (:t, :p, 1, 'DRAFT', CURRENT_DATE) "
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
                (:t, :p, 'minimum_booking_payment_proof', 'minimum_booking_payment_proof',
                 'BOOKING', 'REQUIRED', '{}'::jsonb, 40),
                (:t, :p, 'delivery_placeholder', 'delivery_placeholder',
                 'DELIVERY', 'REQUIRED', '{}'::jsonb, 100)
                """),
            {"t": tenant_id, "p": profile_version_id},
        )
        connection.execute(
            text("UPDATE auditcore.document_requirement_profile_versions "
                 "SET lifecycle_status='PUBLISHED' WHERE tenant_id=:t AND "
                 "document_requirement_profile_version_id=:p"),
            {"t": tenant_id, "p": profile_version_id},
        )
        dealer_id = connection.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"BKT-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"BKT-O-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text("INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, "
                 "customer_type_code, display_name) VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') "
                 "RETURNING customer_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, "
                 "journey_reference, document_requirement_profile_version_id) "
                 "VALUES (:t, :d, :o, :cu, :r, :pv) RETURNING journey_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id,
             "r": f"BKT-J-{suffix}", "pv": profile_version_id},
        ).scalar_one()
        # Starting Booking fires trg_uc03_booking_initialize_requirements,
        # which snapshots the real per-journey journey_document_requirements
        # rows -- no payment receipt has been uploaded, so
        # booking_payment_receipt is left outstanding. The trigger itself
        # renames the profile item's key (minimum_booking_payment_proof,
        # matching the tenant-wide default profile's own naming -- see
        # migration 0022) to booking_payment_receipt on the way in.
        connection.execute(
            text("INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, "
                 "business_status, audit_state, audit_status, first_started_at_utc, "
                 "latest_activity_at_utc, version_no) VALUES (:t, :j, 'BOOKING', "
                 "'BOOKING_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED', now(), now(), 1)"),
            {"t": tenant_id, "j": journey_id},
        )

        rows = _requirement_snapshot(connection, tenant_id=tenant_id, journey_id=journey_id)
        real_keys = {row["requirement_key"] for row in rows}
        assert "booking_payment_receipt" in real_keys, (
            "fixture assumption broken -- real seeding no longer uses this key"
        )

        specs = _booking_requirement_rule_specs(rows)
        assert "BK_MIN_BOOKING_PROOF_PRESENT" in {spec.rule_key for spec in specs}

    engine.dispose()


def _minimal_booking_journey(connection, *, tenant_id: str, suffix: str) -> str:
    """A journey with no document_requirement_profile trigger wiring --
    requirements are inserted directly, for a focused test of one
    requirement's applicability resolution rather than the whole seed."""
    category_id = connection.execute(
        text("INSERT INTO auditcore.product_categories (category_code, category_name) "
             "VALUES (:c, 'V') RETURNING product_category_id"),
        {"c": f"BKC-CAT-{suffix}"},
    ).scalar_one()
    oem_id = connection.execute(
        text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
        {"c": f"BKC-OEM-{suffix}"},
    ).scalar_one()
    connection.execute(
        text("INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id, "
             "product_category_id, effective_start_date) "
             "VALUES (:t, :pc, 'BKC', :o, :cat, CURRENT_DATE)"),
        {"t": tenant_id, "pc": f"BKC-{suffix}", "o": oem_id, "cat": category_id},
    )
    dealer_id = connection.execute(
        text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
             "VALUES (:t, :c, 'D') RETURNING dealer_id"),
        {"t": tenant_id, "c": f"BKC-D-{suffix}"},
    ).scalar_one()
    outlet_id = connection.execute(
        text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
             "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
        {"t": tenant_id, "d": dealer_id, "c": f"BKC-O-{suffix}"},
    ).scalar_one()
    customer_id = connection.execute(
        text("INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, "
             "customer_type_code, display_name) VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') "
             "RETURNING customer_id"),
        {"t": tenant_id, "d": dealer_id, "o": outlet_id},
    ).scalar_one()
    journey_id = connection.execute(
        text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, "
             "journey_reference) VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"),
        {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"BKC-J-{suffix}"},
    ).scalar_one()
    return journey_id


def test_corporate_conditional_requirement_excluded_when_no_corporate_discount_claimed() -> None:
    """Booking never had Delivery's own applicability resolution -- a
    gst_certificate/corporate_id requirement (conditionKey=corporateCustomer)
    stayed outstanding forever on every non-corporate Booking, since nothing
    ever told this rule the condition was known and false. Confirmed
    root-caused live; this proves the fix."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-bkc-{suffix}"
    with engine.begin() as connection:
        journey_id = _minimal_booking_journey(connection, tenant_id=tenant_id, suffix=suffix)
        connection.execute(
            text("""
                INSERT INTO auditcore.journey_document_requirements (
                    tenant_id, journey_id, requirement_key, document_type_key,
                    process_area, requirement_level, condition_snapshot
                ) VALUES (
                    :t, :j, 'gst_certificate', 'gst_certificate', 'BOOKING', 'CONDITIONAL',
                    '{"conditionKey": "corporateCustomer"}'::jsonb
                )
            """),
            {"t": tenant_id, "j": journey_id},
        )

        # No commercial_lines row at all yet -- genuinely unknown, must stay
        # outstanding (never guess NOT_APPLICABLE from silence).
        rows = _requirement_snapshot(connection, tenant_id=tenant_id, journey_id=journey_id)
        specs = _booking_requirement_rule_specs(rows)
        assert "BK_CONDITIONAL_DOCS_ADDRESSED" in {s.rule_key for s in specs}

        # The Booking Form has since synced and confirms no corporate
        # discount was actually claimed -- now resolvable and excluded.
        connection.execute(
            text("INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, actual_amount) "
                 "VALUES (:t, :j, 'corporate_discount_amount', 0)"),
            {"t": tenant_id, "j": journey_id},
        )
        rows = _requirement_snapshot(connection, tenant_id=tenant_id, journey_id=journey_id)
        specs = _booking_requirement_rule_specs(rows)
        assert "BK_CONDITIONAL_DOCS_ADDRESSED" not in {s.rule_key for s in specs}
        assert not rows, "the requirement should be dropped entirely, not merely marked satisfied"

    engine.dispose()


def test_corporate_conditional_requirement_still_flagged_when_discount_actually_claimed() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-bkc2-{suffix}"
    with engine.begin() as connection:
        journey_id = _minimal_booking_journey(connection, tenant_id=tenant_id, suffix=suffix)
        connection.execute(
            text("""
                INSERT INTO auditcore.journey_document_requirements (
                    tenant_id, journey_id, requirement_key, document_type_key,
                    process_area, requirement_level, condition_snapshot
                ) VALUES (
                    :t, :j, 'gst_certificate', 'gst_certificate', 'BOOKING', 'CONDITIONAL',
                    '{"conditionKey": "corporateCustomer"}'::jsonb
                )
            """),
            {"t": tenant_id, "j": journey_id},
        )
        connection.execute(
            text("INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, actual_amount) "
                 "VALUES (:t, :j, 'corporate_discount_amount', 15000)"),
            {"t": tenant_id, "j": journey_id},
        )

        rows = _requirement_snapshot(connection, tenant_id=tenant_id, journey_id=journey_id)
        specs = _booking_requirement_rule_specs(rows)
        by_rule = {s.rule_key: s for s in specs}
        assert "BK_CONDITIONAL_DOCS_ADDRESSED" in by_rule
        assert by_rule["BK_CONDITIONAL_DOCS_ADDRESSED"].requirement_keys == ("gst_certificate",)
        # The description now names the actual outstanding requirement
        # instead of a generic "one or more" sentence.
        assert "gst_certificate" in by_rule["BK_CONDITIONAL_DOCS_ADDRESSED"].description

    engine.dispose()
