"""A pinned SKU's model and variant names stay on the journey (product owner 2026-11-14: "its all SKU")."""
from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_post_extraction_materialization as post_extract
from audit_core.db import set_tenant_context


@pytest.fixture
def journey():
    engine = database_engine()
    j = create_p2_journey(engine, prefix="pin")
    try:
        yield j
    finally:
        delete_tenant_data(engine, j.tenant_id)
        engine.dispose()


def _sku(connection) -> str:
    suffix = uuid4().hex[:8]
    oem = connection.execute(text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
                             {"c": f"PIN-OEM-{suffix}"}).scalar_one()
    model = connection.execute(text("INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
                                    "VALUES (:o, :c, 'NEW SCORPIO N') RETURNING model_id"),
                               {"o": oem, "c": f"PIN-M-{suffix}"}).scalar_one()
    variant = connection.execute(text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
                                      "VALUES (:m, :c, 'Z8 S D MT 2WD 7 STR') RETURNING variant_id"),
                                 {"m": model, "c": f"PIN-V-{suffix}"}).scalar_one()
    return str(connection.execute(
        text("INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) VALUES (:o, :m, :v, :c) "
             "RETURNING product_sku_id"), {"o": oem, "m": model, "v": variant, "c": f"PIN-S-{suffix}"}).scalar_one())


def _names(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return tuple(connection.execute(
            text("SELECT model_name_snapshot, variant_name_snapshot, colour_name_snapshot FROM auditcore.journey_products "
                 "WHERE tenant_id=:t AND journey_id=:j"), {"t": journey.tenant_id, "j": journey.journey_id}).one())


def test_document_text_never_writes_over_a_pinned_skus_names(journey):
    semantics = post_extract._PRODUCT_SEMANTICS
    assert {"model_name_snapshot", "variant_name_snapshot", "colour_name_snapshot"} <= set(semantics.values())
    keys = {column: key for key, column in semantics.items()}
    resolved = lambda model, variant, colour: {keys["model_name_snapshot"]: {"value": model},
                                               keys["variant_name_snapshot"]: {"value": variant},
                                               keys["colour_name_snapshot"]: {"value": colour}}

    def read(model, variant, colour):
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            post_extract._materialize_product(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                              resolved=resolved(model, variant, colour))

    # Before a pin the document's text is the journey's.
    read("Scorpion N", "Z8S DMT", "Everest White")
    assert _names(journey) == ("Scorpion N", "Z8S DMT", "Everest White")
    with journey.engine.begin() as connection:
        sku = _sku(connection)
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.journey_products SET product_sku_id=:s, model_name_snapshot='NEW SCORPIO N', "
                                "variant_name_snapshot='Z8 S D MT 2WD 7 STR', selection_source='EVIDENCE' "
                                "WHERE tenant_id=:t AND journey_id=:j"), {"s": sku, "t": journey.tenant_id, "j": journey.journey_id})
    # Pinned: another document's text does not write over the master's names; the colour still follows.
    read("SCORPIO-N", "Z8L DMT", "Stealth Black")
    assert _names(journey) == ("NEW SCORPIO N", "Z8 S D MT 2WD 7 STR", "Stealth Black")
