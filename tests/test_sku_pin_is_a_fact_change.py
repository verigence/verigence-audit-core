"""Pinning a SKU is a fact change like any other (real Postgres).

Found live 2026-10-24: a resolution run re-pinned SKUs overnight, which
rewrites every standard amount on the deal, but ``journey_products`` was
written on its own. The Journey's ``fact_version`` never moved, so the Journey
list's cached variance and the controls' ``evaluated_fact_version`` both still
believed themselves current -- one customer read +2,71,181 on the list against
-9,407 on the Deal tab, from the same field.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_model_resolution as resolution
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_runtime import note_facts_changed


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="skufc")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _a_priced_sku(journey) -> UUID:
    suffix = uuid4().hex[:8]
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        oem_id = connection.execute(
            text("SELECT oem_id FROM auditcore.projects WHERE tenant_id = :t"),
            {"t": journey.tenant_id},
        ).scalar_one()
        model_id = connection.execute(
            text("INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
                 "VALUES (:o, :c, 'SCORPIO N') RETURNING model_id"),
            {"o": oem_id, "c": f"FC-M-{suffix}"},
        ).scalar_one()
        variant_id = connection.execute(
            text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
                 "VALUES (:m, :c, 'Z8L D MT 2WD') RETURNING variant_id"),
            {"m": model_id, "c": f"FC-V-{suffix}"},
        ).scalar_one()
        return connection.execute(
            text("INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) "
                 "VALUES (:o, :m, :v, :c) RETURNING product_sku_id"),
            {"o": oem_id, "m": model_id, "v": variant_id, "c": f"FC-SKU-{suffix}"},
        ).scalar_one()


def _runtime(journey) -> tuple[int, int] | None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        row = connection.execute(
            text("SELECT fact_version FROM auditcore.p2_journey_runtime "
                 "WHERE tenant_id = :t AND journey_id = :j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).first()
    return None if row is None else int(row[0])


def _recomputes_queued(journey) -> int:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT COUNT(*) FROM auditcore.p2_work_queue "
                 "WHERE tenant_id = :t AND journey_id = :j AND work_type = 'STAGE_RECOMPUTE'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()


def test_an_automatic_pin_moves_the_fact_version_and_asks_for_a_recompute(journey):
    sku_id = _a_priced_sku(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        note_facts_changed(connection, tenant_id=journey.tenant_id,
                           journey_id=journey.journey_id, reason="TEST_SETUP")
    before = _runtime(journey)
    assert before is not None

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert resolution._pin_sku(connection, tenant_id=journey.tenant_id,
                                   journey_id=journey.journey_id, product_sku_id=sku_id) is True

    assert _runtime(journey) > before, "the pin changed the deal's standard amounts silently"
    assert _recomputes_queued(journey) == 1


def test_a_reviewers_own_pin_does_the_same(journey):
    sku_id = _a_priced_sku(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        note_facts_changed(connection, tenant_id=journey.tenant_id,
                           journey_id=journey.journey_id, reason="TEST_SETUP")
    before = _runtime(journey)

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        resolution._pin_human(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                              product_sku_id=sku_id, method="MANUAL",
                              basis=resolution.BASIS_TL, actor_id=journey.actor_id)

    assert _runtime(journey) > before


def test_a_pin_never_turns_a_phase_one_journey_into_a_phase_two_one(journey):
    """``p2_request_stage_recompute`` creates the runtime row it bumps, and the
    list reads that row's presence as "this is a Phase 2 journey". A journey
    that has none keeps none."""
    sku_id = _a_priced_sku(journey)
    assert _runtime(journey) is None

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert resolution._pin_sku(connection, tenant_id=journey.tenant_id,
                                   journey_id=journey.journey_id, product_sku_id=sku_id) is True

    assert _runtime(journey) is None
    assert _recomputes_queued(journey) == 0
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert connection.execute(
            text("SELECT product_sku_id FROM auditcore.journey_products "
                 "WHERE tenant_id = :t AND journey_id = :j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one() == sku_id, "the pin itself must still be written"
