from __future__ import annotations

import inspect
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_pc_booking_documents as pc_documents
from audit_core.uc03_delivery_documents import (
    _resolve_known_applicability,
    resolve_requirement_applicability_if_conditional,
)


@pytest.fixture
def delivery_journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-dlapp-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DA-CAT-{suffix[:8]}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DA-OEM-{suffix[:8]}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id, effective_start_date)
                VALUES (:t, :pc, 'DA', :o, :cat, CURRENT_DATE - 60)"""),
            {"t": tenant_id, "pc": f"DA-{suffix[:8]}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DA-D-{suffix[:8]}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DA-O-{suffix[:8]}"},
        ).scalar_one()
        customer_id = c.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DA-J-{suffix[:8]}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'DELIVERY', 'DELIVERY_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _conditional_requirement(c, *, requirement_key, condition_key, document_type_key=None):
    return c.execute(
        text("""
            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, requirement_key, document_type_key,
                process_area, requirement_level, condition_snapshot
            ) VALUES (
                :t, :j, :key, :doc_type, 'DELIVERY', 'CONDITIONAL',
                CAST(:snapshot AS jsonb)
            ) RETURNING journey_document_requirement_id
        """),
        {
            "t": c.tenant_id, "j": c.journey_id, "key": requirement_key,
            "doc_type": document_type_key or requirement_key,
            "snapshot": f'{{"conditionKey":"{condition_key}"}}',
        },
    ).scalar_one()


def test_accessories_taken_resolves_once_the_commercial_line_lands(delivery_journey) -> None:
    """Regression: only exchangeTaken was ever resolved for Delivery conditional
    requirements -- accessoriesTaken, extendedWarrantyTaken, rsaTaken and
    registrationByDealer stayed UNRESOLVED forever, which meant DI's document-
    link webhook rejected every callback for those document types with a
    permanent 409 (VAC-CONFLICT-004), confirmed directly against live Railway
    logs showing the same callback retried 40+ times, always failing."""
    c = delivery_journey
    requirement_id = _conditional_requirement(
        c, requirement_key="accessory_invoice_dms", condition_key="accessoriesTaken",
    )

    # Before the commercial fact lands: stays UNRESOLVED (never guesses).
    _resolve_known_applicability(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    snapshot = c.execute(
        text("SELECT condition_snapshot, requirement_status FROM auditcore.journey_document_requirements "
             "WHERE journey_document_requirement_id=:r"),
        {"r": requirement_id},
    ).mappings().one()
    assert snapshot["condition_snapshot"].get("applicabilityState") is None
    assert snapshot["requirement_status"] == "PENDING"

    c.execute(
        text("""INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, actual_amount)
                VALUES (:t, :j, 'accessories_cost', 5000)"""),
        {"t": c.tenant_id, "j": c.journey_id},
    )

    _resolve_known_applicability(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    snapshot = c.execute(
        text("SELECT condition_snapshot, requirement_status FROM auditcore.journey_document_requirements "
             "WHERE journey_document_requirement_id=:r"),
        {"r": requirement_id},
    ).mappings().one()
    assert snapshot["condition_snapshot"]["applicabilityState"] == "APPLICABLE"
    assert snapshot["requirement_status"] == "PENDING"


def test_extended_warranty_and_rsa_resolve_not_applicable_at_zero_amount(delivery_journey) -> None:
    c = delivery_journey
    ew_id = _conditional_requirement(c, requirement_key="ew_invoice", condition_key="extendedWarrantyTaken")
    rsa_id = _conditional_requirement(c, requirement_key="rsa_invoice", condition_key="rsaTaken")
    c.execute(
        text("""INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, actual_amount)
                VALUES (:t, :j, 'additional_warranty_amount', 0), (:t, :j, 'rsa_amount', 0)"""),
        {"t": c.tenant_id, "j": c.journey_id},
    )

    _resolve_known_applicability(c, tenant_id=c.tenant_id, journey_id=c.journey_id)

    for req_id in (ew_id, rsa_id):
        snapshot = c.execute(
            text("SELECT condition_snapshot, requirement_status FROM auditcore.journey_document_requirements "
                 "WHERE journey_document_requirement_id=:r"),
            {"r": req_id},
        ).mappings().one()
        assert snapshot["condition_snapshot"]["applicabilityState"] == "NOT_APPLICABLE"
        assert snapshot["requirement_status"] == "NOT_APPLICABLE"


def test_registration_by_dealer_resolves_from_extracted_field(delivery_journey) -> None:
    c = delivery_journey
    requirement_id = _conditional_requirement(
        c, requirement_key="rto_challan", condition_key="registrationByDealer",
    )
    c.execute(
        text("""
            INSERT INTO auditcore.journey_document_extracted_fields (
                tenant_id, journey_id, stage_code, di_document_id, field_key,
                extracted_value, effective_value, is_modified
            ) VALUES (
                :t, :j, 'BOOKING', :doc, 'registration_by',
                CAST('"Dealer"' AS jsonb), CAST('"Dealer"' AS jsonb), false
            )
        """),
        {"t": c.tenant_id, "j": c.journey_id, "doc": uuid4()},
    )

    _resolve_known_applicability(c, tenant_id=c.tenant_id, journey_id=c.journey_id)

    snapshot = c.execute(
        text("SELECT condition_snapshot FROM auditcore.journey_document_requirements "
             "WHERE journey_document_requirement_id=:r"),
        {"r": requirement_id},
    ).mappings().one()
    assert snapshot["condition_snapshot"]["applicabilityState"] == "APPLICABLE"


def test_single_row_resolver_only_touches_its_own_requirement(delivery_journey) -> None:
    """The webhook's own resolver must not need or take a lock on any other
    conditional requirement -- given one row's own already-fetched mapping,
    it resolves and updates only that row, leaving a second, unrelated
    conditional requirement (still unresolvable) completely untouched."""
    c = delivery_journey
    accessories_id = _conditional_requirement(
        c, requirement_key="accessory_invoice_dms", condition_key="accessoriesTaken",
    )
    other_id = _conditional_requirement(c, requirement_key="ew_invoice", condition_key="extendedWarrantyTaken")
    c.execute(
        text("""INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, actual_amount)
                VALUES (:t, :j, 'accessories_cost', 3000)"""),
        {"t": c.tenant_id, "j": c.journey_id},
    )
    requirement = c.execute(
        text("SELECT journey_document_requirement_id, requirement_level, condition_snapshot "
             "FROM auditcore.journey_document_requirements WHERE journey_document_requirement_id=:r"),
        {"r": accessories_id},
    ).mappings().one()

    updated = resolve_requirement_applicability_if_conditional(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, requirement=requirement,
    )

    assert updated is not None
    assert updated["condition_snapshot"]["applicabilityState"] == "APPLICABLE"
    assert updated["requirement_status"] == "PENDING"

    other = c.execute(
        text("SELECT condition_snapshot FROM auditcore.journey_document_requirements WHERE journey_document_requirement_id=:r"),
        {"r": other_id},
    ).mappings().one()
    assert other["condition_snapshot"].get("applicabilityState") is None

    # A non-conditional or already-resolved requirement is a clean no-op.
    assert resolve_requirement_applicability_if_conditional(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id,
        requirement={"requirement_level": "REQUIRED", "condition_snapshot": {}},
    ) is None


def test_document_link_webhook_resolves_only_its_own_row_before_gating() -> None:
    # Source-inspected rather than exercised end-to-end through the full HTTP
    # webhook (service-principal auth, DI subject mapping, evidence creation
    # are a large fixture that adds nothing to this specific assertion): the
    # webhook must self-heal a stuck-UNRESOLVED Delivery requirement before
    # _require_callback_applicable can 409 it, but ONLY via the single-row
    # resolver -- NOT _resolve_known_applicability, whose journey-wide
    # ``FOR UPDATE`` caused a live lock-contention incident when called from
    # every callback (see resolve_requirement_applicability_if_conditional's
    # docstring). Asserting the safe function is used, and the unsafe one
    # is not, keeps that regression from silently coming back.
    source = inspect.getsource(pc_documents.acknowledge_booking_document_link)
    assert "DELIVERY" in source
    assert "resolve_requirement_applicability_if_conditional(" in source
    assert "_resolve_known_applicability(" not in source
