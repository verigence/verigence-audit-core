"""The SKU run's own totals must account for every Journey (real Postgres).

Found 2026-10-24 while reconciling a run report: the run logged 31 Journeys
without a SKU where 46 of Mahindra Orissa's 171 had none. _sku_counts counted
from journey_products, and a Journey whose documents never yielded any vehicle
words has no row there at all -- so the fifteen hardest cases were missing
from the very number meant to show them.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_backfill_document_sync_producers import _sku_counts


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="skuct")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _another_journey(journey) -> UUID:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, "
                 "journey_reference) VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"),
            {"t": journey.tenant_id, "d": journey.dealer_id, "o": journey.outlet_id,
             "cu": journey.customer_id, "r": f"CT-J-{uuid4().hex[:8]}"},
        ).scalar_one()


def _product_row(journey, journey_id, *, with_sku: bool) -> None:
    suffix = uuid4().hex[:8]
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        sku_id = None
        if with_sku:
            oem_id = connection.execute(
                text("SELECT oem_id FROM auditcore.projects WHERE tenant_id = :t"),
                {"t": journey.tenant_id},
            ).scalar_one()
            model_id = connection.execute(
                text("INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
                     "VALUES (:o, :c, 'THAR ROXX') RETURNING model_id"),
                {"o": oem_id, "c": f"CT-M-{suffix}"},
            ).scalar_one()
            variant_id = connection.execute(
                text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
                     "VALUES (:m, :c, 'MX1 D MT RWD') RETURNING variant_id"),
                {"m": model_id, "c": f"CT-V-{suffix}"},
            ).scalar_one()
            sku_id = connection.execute(
                text("INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) "
                     "VALUES (:o, :m, :v, :c) RETURNING product_sku_id"),
                {"o": oem_id, "m": model_id, "v": variant_id, "c": f"CT-S-{suffix}"},
            ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.journey_products (tenant_id, journey_id, product_sku_id, "
                 "model_name_snapshot, selection_source) VALUES (:t, :j, :s, 'THAR ROXX', 'EVIDENCE')"),
            {"t": journey.tenant_id, "j": journey_id, "s": sku_id},
        )


def test_a_journey_nothing_was_read_for_is_counted_as_without_a_sku(journey):
    """The Journey that has no product row at all is the one the number most
    needs to show: nothing about its vehicle was read."""
    resolved = _another_journey(journey)
    _product_row(journey, resolved, with_sku=True)
    # journey.journey_id gets no product row, as a Journey whose documents
    # yielded no vehicle words has none.
    assert _sku_counts(journey.engine, tenant_id=journey.tenant_id) == (1, 1)


def test_a_product_row_without_a_sku_still_counts_as_without_one(journey):
    _product_row(journey, journey.journey_id, with_sku=False)
    unread = _another_journey(journey)
    resolved = _another_journey(journey)
    _product_row(journey, resolved, with_sku=True)
    assert unread is not None
    with_sku, without_sku = _sku_counts(journey.engine, tenant_id=journey.tenant_id)
    assert (with_sku, without_sku) == (1, 2)
    assert with_sku + without_sku == 3, "every Journey is in exactly one of the two figures"
