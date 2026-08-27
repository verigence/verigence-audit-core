import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.price_lists import (
    add_price_list_item,
    create_price_list,
    create_price_list_version,
    get_price_matrix,
    publish_price_list_version,
    resolve_effective_price_plan,
    retire_price_list_version,
)
from audit_core.product_catalogue import create_model, create_oem, create_sku, create_variant


def test_runtime_price_plan_uses_business_date_and_returns_sku_matrix() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for runtime pricing integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-runtime-price-{suffix}"
    try:
        with engine.begin() as connection:
            oem_id = create_oem(connection, code=f"RPEOM-{suffix}", name="Runtime Price OEM")
            model_id = create_model(
                connection,
                oem_id=oem_id,
                code=f"RPM-{suffix}",
                name="Runtime Price Model",
            )
            variant_id = create_variant(
                connection,
                model_id=model_id,
                code=f"RPV-{suffix}",
                name="Runtime Price Variant",
            )
            sku_id = create_sku(
                connection,
                oem_id=oem_id,
                model_id=model_id,
                variant_id=variant_id,
                colour_id=None,
                sku_code=f"RPSKU-{suffix}",
            )
            category_id = connection.execute(
                text(
                    "INSERT INTO auditcore.product_categories (category_code, category_name) "
                    "VALUES (:code, 'Vehicle') RETURNING product_category_id"
                ),
                {"code": f"RPCAT-{suffix}"},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.projects (
                        tenant_id, project_code, project_name, oem_id,
                        product_category_id, effective_start_date
                    ) VALUES (
                        :tenant_id, :code, 'Runtime Price Project', :oem_id,
                        :category_id, CURRENT_DATE
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "code": f"RPP-{suffix}",
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

            august_version_id = create_price_list_version(
                connection,
                tenant_id=tenant_id,
                price_list_id=price_list_id,
                version_no=1,
                effective_from=date(2026, 8, 1),
                effective_to=date(2026, 8, 31),
                actor_id="admin",
            )
            add_price_list_item(
                connection,
                tenant_id=tenant_id,
                price_list_version_id=august_version_id,
                product_sku_id=sku_id,
                component_key="EX_SHOWROOM",
                standard_amount=Decimal("100000.00"),
            )
            add_price_list_item(
                connection,
                tenant_id=tenant_id,
                price_list_version_id=august_version_id,
                product_sku_id=sku_id,
                component_key="TCS",
                standard_amount=Decimal("1000.00"),
            )
            publish_price_list_version(
                connection,
                tenant_id=tenant_id,
                price_list_version_id=august_version_id,
                actor_id="admin",
            )
            retire_price_list_version(
                connection,
                tenant_id=tenant_id,
                price_list_version_id=august_version_id,
                actor_id="admin",
            )

            september_version_id = create_price_list_version(
                connection,
                tenant_id=tenant_id,
                price_list_id=price_list_id,
                version_no=2,
                effective_from=date(2026, 9, 1),
                actor_id="admin",
            )
            add_price_list_item(
                connection,
                tenant_id=tenant_id,
                price_list_version_id=september_version_id,
                product_sku_id=sku_id,
                component_key="EX_SHOWROOM",
                standard_amount=Decimal("105000.00"),
            )
            publish_price_list_version(
                connection,
                tenant_id=tenant_id,
                price_list_version_id=september_version_id,
                actor_id="admin",
            )

            august_plan = resolve_effective_price_plan(
                connection,
                tenant_id=tenant_id,
                effective_on=date(2026, 8, 31),
            )
            september_plan = resolve_effective_price_plan(
                connection,
                tenant_id=tenant_id,
                effective_on=date(2026, 9, 1),
            )

            assert august_plan["price_list_version_id"] == august_version_id
            assert august_plan["lifecycle_status"] == "RETIRED"
            assert september_plan["price_list_version_id"] == september_version_id
            assert september_plan["lifecycle_status"] == "PUBLISHED"

            august_matrix = get_price_matrix(
                connection,
                tenant_id=tenant_id,
                price_list_version_id=august_version_id,
                product_sku_id=sku_id,
            )
            september_matrix = get_price_matrix(
                connection,
                tenant_id=tenant_id,
                price_list_version_id=september_version_id,
                product_sku_id=sku_id,
            )

            assert [row["component_key"] for row in august_matrix] == ["EX_SHOWROOM", "TCS"]
            assert august_matrix[0]["standard_amount"] == Decimal("100000.00")
            assert september_matrix[0]["standard_amount"] == Decimal("105000.00")
    finally:
        engine.dispose()
