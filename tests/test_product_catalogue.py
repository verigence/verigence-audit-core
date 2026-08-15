import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from audit_core.product_catalogue import (
    create_colour,
    create_model,
    create_oem,
    create_sku,
    create_variant,
    resolve_sellable_configuration,
)


def test_product_catalogue_resolves_sellable_configuration() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for product catalogue integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    try:
        with engine.begin() as connection:
            oem_id = create_oem(connection, code=f"OEM-{suffix}", name="Test OEM")
            model_id = create_model(
                connection,
                oem_id=oem_id,
                code=f"MODEL-{suffix}",
                name="Test Model",
            )
            variant_id = create_variant(
                connection,
                model_id=model_id,
                code=f"VARIANT-{suffix}",
                name="Test Variant",
            )
            colour_id = create_colour(
                connection,
                oem_id=oem_id,
                code=f"COLOUR-{suffix}",
                name="Test Colour",
            )
            sku_id = create_sku(
                connection,
                oem_id=oem_id,
                model_id=model_id,
                variant_id=variant_id,
                colour_id=colour_id,
                sku_code=f"SKU-{suffix}",
            )

            resolved = resolve_sellable_configuration(
                connection,
                product_sku_id=sku_id,
            )

            assert resolved["product_sku_id"] == sku_id
            assert resolved["oem_id"] == oem_id
            assert resolved["model_id"] == model_id
            assert resolved["variant_id"] == variant_id
            assert resolved["colour_id"] == colour_id
    finally:
        engine.dispose()
