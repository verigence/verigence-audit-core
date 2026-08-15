import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from audit_core.price_lists import (
    add_price_list_item,
    create_price_list,
    create_price_list_version,
    publish_price_list_version,
    resolve_effective_price_list_version,
    retire_price_list_version,
)
from audit_core.product_catalogue import (
    create_model,
    create_oem,
    create_sku,
    create_variant,
)


def test_price_list_publish_resolve_immutable_and_retire() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for Price List integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-price-{suffix}"
    try:
        with engine.begin() as connection:
            oem_id = create_oem(connection, code=f"PEOM-{suffix}", name="Price OEM")
            model_id = create_model(
                connection,
                oem_id=oem_id,
                code=f"PM-{suffix}",
                name="Price Model",
            )
            variant_id = create_variant(
                connection,
                model_id=model_id,
                code=f"PV-{suffix}",
                name="Price Variant",
            )
            sku_id = create_sku(
                connection,
                oem_id=oem_id,
                model_id=model_id,
                variant_id=variant_id,
                colour_id=None,
                sku_code=f"PSKU-{suffix}",
            )
            category_id = connection.execute(
                text(
                    "INSERT INTO auditcore.product_categories (category_code, category_name) "
                    "VALUES (:code, 'Vehicle') RETURNING product_category_id"
                ),
                {"code": f"PCAT-{suffix}"},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.projects (
                        tenant_id, project_code, project_name, oem_id,
                        product_category_id, effective_start_date
                    ) VALUES (
                        :tenant_id, :code, 'Price Project', :oem_id,
                        :category_id, CURRENT_DATE
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "code": f"PP-{suffix}",
                    "oem_id": oem_id,
                    "category_id": category_id,
                },
            )

            price_list_id = create_price_list(
                connection,
                tenant_id=tenant_id,
                code="RETAIL",
                name="Retail Price List",
                actor_id="admin",
            )
            version_id = create_price_list_version(
                connection,
                tenant_id=tenant_id,
                price_list_id=price_list_id,
                version_no=1,
                effective_from=date(2026, 8, 1),
                actor_id="admin",
            )
            item_id = add_price_list_item(
                connection,
                tenant_id=tenant_id,
                price_list_version_id=version_id,
                product_sku_id=sku_id,
                component_key="EX_SHOWROOM",
                standard_amount=Decimal("100000.00"),
            )

            connection.execute(
                text(
                    "UPDATE auditcore.price_list_items SET standard_amount = 101000 "
                    "WHERE tenant_id = :tenant_id AND price_list_item_id = :item_id"
                ),
                {"tenant_id": tenant_id, "item_id": item_id},
            )
            publish_price_list_version(
                connection,
                tenant_id=tenant_id,
                price_list_version_id=version_id,
                actor_id="admin",
            )

            assert resolve_effective_price_list_version(
                connection,
                tenant_id=tenant_id,
                price_list_id=price_list_id,
                effective_on=date(2026, 8, 15),
            ) == version_id

            with (
                pytest.raises(DBAPIError, match="child rows may only be changed"),
                connection.begin_nested(),
            ):
                connection.execute(
                    text(
                        "UPDATE auditcore.price_list_items SET standard_amount = 102000 "
                        "WHERE tenant_id = :tenant_id AND price_list_item_id = :item_id"
                    ),
                    {"tenant_id": tenant_id, "item_id": item_id},
                )

            retire_price_list_version(
                connection,
                tenant_id=tenant_id,
                price_list_version_id=version_id,
                actor_id="admin",
            )
            lifecycle = connection.execute(
                text(
                    "SELECT lifecycle_status FROM auditcore.price_list_versions "
                    "WHERE tenant_id = :tenant_id AND price_list_version_id = :version_id"
                ),
                {"tenant_id": tenant_id, "version_id": version_id},
            ).scalar_one()
            assert lifecycle == "RETIRED"
    finally:
        engine.dispose()
