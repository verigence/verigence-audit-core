import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from audit_core.discount_schemes import (
    add_discount_benefit,
    add_discount_eligibility,
    create_discount_scheme,
    create_discount_scheme_version,
    publish_discount_scheme_version,
    retire_discount_scheme_version,
)
from audit_core.product_catalogue import (
    create_model,
    create_oem,
    create_sku,
    create_variant,
)


def test_discount_scheme_lifecycle_keeps_configured_benefits_immutable() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for Discount Scheme integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-discount-{suffix}"
    try:
        with engine.begin() as connection:
            oem_id = create_oem(connection, code=f"DEOM-{suffix}", name="Discount OEM")
            model_id = create_model(
                connection,
                oem_id=oem_id,
                code=f"DM-{suffix}",
                name="Discount Model",
            )
            variant_id = create_variant(
                connection,
                model_id=model_id,
                code=f"DV-{suffix}",
                name="Discount Variant",
            )
            sku_id = create_sku(
                connection,
                oem_id=oem_id,
                model_id=model_id,
                variant_id=variant_id,
                colour_id=None,
                sku_code=f"DSKU-{suffix}",
            )
            category_id = connection.execute(
                text(
                    "INSERT INTO auditcore.product_categories (category_code, category_name) "
                    "VALUES (:code, 'Vehicle') RETURNING product_category_id"
                ),
                {"code": f"DCAT-{suffix}"},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.projects (
                        tenant_id, project_code, project_name, oem_id,
                        product_category_id, effective_start_date
                    ) VALUES (
                        :tenant_id, :code, 'Discount Project', :oem_id,
                        :category_id, CURRENT_DATE
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "code": f"DP-{suffix}",
                    "oem_id": oem_id,
                    "category_id": category_id,
                },
            )

            scheme_id = create_discount_scheme(
                connection,
                tenant_id=tenant_id,
                code="RETAIL-OFFER",
                name="Retail Offer",
                actor_id="admin",
            )
            version_id = create_discount_scheme_version(
                connection,
                tenant_id=tenant_id,
                discount_scheme_id=scheme_id,
                version_no=1,
                effective_from=date(2026, 8, 1),
                actor_id="admin",
            )
            add_discount_eligibility(
                connection,
                tenant_id=tenant_id,
                discount_scheme_version_id=version_id,
                product_sku_id=sku_id,
                customer_type_code="RETAIL",
            )
            benefit_id = add_discount_benefit(
                connection,
                tenant_id=tenant_id,
                discount_scheme_version_id=version_id,
                benefit_key="CONFIGURED_CASH_BENEFIT",
                benefit_type="AMOUNT",
                amount_value=Decimal("10000.00"),
            )

            connection.execute(
                text(
                    "UPDATE auditcore.discount_scheme_benefits SET amount_value = 11000 "
                    "WHERE tenant_id = :tenant_id AND benefit_id = :benefit_id"
                ),
                {"tenant_id": tenant_id, "benefit_id": benefit_id},
            )
            publish_discount_scheme_version(
                connection,
                tenant_id=tenant_id,
                discount_scheme_version_id=version_id,
                actor_id="admin",
            )

            with (
                pytest.raises(DBAPIError, match="child rows may only be changed"),
                connection.begin_nested(),
            ):
                connection.execute(
                    text(
                        "UPDATE auditcore.discount_scheme_benefits SET amount_value = 12000 "
                        "WHERE tenant_id = :tenant_id AND benefit_id = :benefit_id"
                    ),
                    {"tenant_id": tenant_id, "benefit_id": benefit_id},
                )

            stored_amount = connection.execute(
                text(
                    "SELECT amount_value FROM auditcore.discount_scheme_benefits "
                    "WHERE tenant_id = :tenant_id AND benefit_id = :benefit_id"
                ),
                {"tenant_id": tenant_id, "benefit_id": benefit_id},
            ).scalar_one()
            assert stored_amount == Decimal("11000.00")

            retire_discount_scheme_version(
                connection,
                tenant_id=tenant_id,
                discount_scheme_version_id=version_id,
                actor_id="admin",
            )
            lifecycle = connection.execute(
                text(
                    "SELECT lifecycle_status FROM auditcore.discount_scheme_versions "
                    "WHERE tenant_id = :tenant_id AND discount_scheme_version_id = :version_id"
                ),
                {"tenant_id": tenant_id, "version_id": version_id},
            ).scalar_one()
            assert lifecycle == "RETIRED"
    finally:
        engine.dispose()
