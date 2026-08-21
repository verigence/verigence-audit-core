import os
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from audit_core.errors import AuditCoreError
from audit_core.product_masters import (
    add_project_product_master_item,
    create_project_product_master_version,
    product_sku_is_in_effective_master,
    publish_project_product_master_version,
    resolve_effective_project_product_master_version,
    retire_project_product_master_version,
)


def _seed_project_and_sku(connection, *, suffix: str, tenant_id: str):
    category_id = connection.execute(
        text(
            "INSERT INTO auditcore.product_categories (category_code, category_name) "
            "VALUES (:code, :name) RETURNING product_category_id"
        ),
        {"code": f"PM-CAT-{suffix}", "name": f"Product Category {suffix}"},
    ).scalar_one()
    oem_id = connection.execute(
        text(
            "INSERT INTO auditcore.oems (oem_code, oem_name) "
            "VALUES (:code, :name) RETURNING oem_id"
        ),
        {"code": f"PM-OEM-{suffix}", "name": f"Product OEM {suffix}"},
    ).scalar_one()
    model_id = connection.execute(
        text(
            "INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
            "VALUES (:oem_id, :code, :name) RETURNING model_id"
        ),
        {"oem_id": oem_id, "code": f"MODEL-{suffix}", "name": "Model"},
    ).scalar_one()
    variant_id = connection.execute(
        text(
            "INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
            "VALUES (:model_id, :code, :name) RETURNING variant_id"
        ),
        {"model_id": model_id, "code": f"VAR-{suffix}", "name": "Variant"},
    ).scalar_one()
    colour_id = connection.execute(
        text(
            "INSERT INTO auditcore.colours (oem_id, colour_code, colour_name) "
            "VALUES (:oem_id, :code, :name) RETURNING colour_id"
        ),
        {"oem_id": oem_id, "code": f"CLR-{suffix}", "name": "Colour"},
    ).scalar_one()
    sku_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.product_skus (
                oem_id, model_id, variant_id, colour_id, sku_code,
                attributes
            ) VALUES (
                :oem_id, :model_id, :variant_id, :colour_id, :sku_code,
                jsonb_build_object('source', 'uc02-test')
            ) RETURNING product_sku_id
            """
        ),
        {
            "oem_id": oem_id,
            "model_id": model_id,
            "variant_id": variant_id,
            "colour_id": colour_id,
            "sku_code": f"SKU-{suffix}",
        },
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.projects (
                tenant_id, project_code, project_name, oem_id,
                product_category_id, effective_start_date, project_status
            ) VALUES (
                :tenant_id, :code, :name, :oem_id,
                :category_id, DATE '2026-08-01', 'CONFIGURING'
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "code": f"PM-PROJ-{suffix}",
            "name": f"Product Master Project {suffix}",
            "oem_id": oem_id,
            "category_id": category_id,
        },
    )
    return sku_id


def test_project_product_master_latest_wef_wins_and_history_is_immutable() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC02 Product Master integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-product-master-{suffix}"
    try:
        with engine.begin() as connection:
            sku_id = _seed_project_and_sku(
                connection,
                suffix=suffix,
                tenant_id=tenant_id,
            )
            first_version = create_project_product_master_version(
                connection,
                tenant_id=tenant_id,
                effective_from=date(2026, 8, 1),
                actor_id="superadmin",
            )
            add_project_product_master_item(
                connection,
                tenant_id=tenant_id,
                version_id=first_version,
                product_sku_id=sku_id,
                source_import_row_no=2,
            )
            publish_project_product_master_version(
                connection,
                tenant_id=tenant_id,
                version_id=first_version,
                actor_id="superadmin",
            )

            second_version = create_project_product_master_version(
                connection,
                tenant_id=tenant_id,
                effective_from=date(2026, 8, 15),
                actor_id="superadmin",
            )
            add_project_product_master_item(
                connection,
                tenant_id=tenant_id,
                version_id=second_version,
                product_sku_id=sku_id,
                source_import_row_no=2,
            )
            publish_project_product_master_version(
                connection,
                tenant_id=tenant_id,
                version_id=second_version,
                actor_id="superadmin",
            )

            assert resolve_effective_project_product_master_version(
                connection,
                tenant_id=tenant_id,
                effective_on=date(2026, 8, 10),
            ) == first_version
            assert resolve_effective_project_product_master_version(
                connection,
                tenant_id=tenant_id,
                effective_on=date(2026, 8, 20),
            ) == second_version
            assert product_sku_is_in_effective_master(
                connection,
                tenant_id=tenant_id,
                product_sku_id=sku_id,
                effective_on=date(2026, 8, 20),
            ) is True

            snapshot = connection.execute(
                text(
                    """
                    SELECT approved_product_snapshot
                    FROM auditcore.project_product_master_items
                    WHERE tenant_id=:tenant_id AND version_id=:version_id
                    """
                ),
                {"tenant_id": tenant_id, "version_id": second_version},
            ).scalar_one()
            assert snapshot["product_sku_id"] == str(sku_id)
            assert snapshot["sku_code"] == f"SKU-{suffix}"

            with (
                pytest.raises(DBAPIError, match="published master version can only be retired"),
                connection.begin_nested(),
            ):
                connection.execute(
                    text(
                        """
                        UPDATE auditcore.project_product_master_versions
                        SET effective_from=DATE '2026-08-16'
                        WHERE tenant_id=:tenant_id AND version_id=:version_id
                        """
                    ),
                    {"tenant_id": tenant_id, "version_id": second_version},
                )

            with (
                pytest.raises(DBAPIError, match="child rows may only be changed"),
                connection.begin_nested(),
            ):
                connection.execute(
                    text(
                        """
                        UPDATE auditcore.project_product_master_items
                        SET approved_product_snapshot=jsonb_build_object('changed', true)
                        WHERE tenant_id=:tenant_id AND version_id=:version_id
                        """
                    ),
                    {"tenant_id": tenant_id, "version_id": second_version},
                )

            retire_project_product_master_version(
                connection,
                tenant_id=tenant_id,
                version_id=first_version,
                actor_id="superadmin",
            )
    finally:
        engine.dispose()


def test_same_latest_wef_is_reported_as_configuration_conflict() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC02 Product Master integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-product-master-tie-{suffix}"
    try:
        with engine.begin() as connection:
            sku_id = _seed_project_and_sku(
                connection,
                suffix=suffix,
                tenant_id=tenant_id,
            )
            for _ in range(2):
                version_id = create_project_product_master_version(
                    connection,
                    tenant_id=tenant_id,
                    effective_from=date(2026, 8, 15),
                    actor_id="superadmin",
                )
                add_project_product_master_item(
                    connection,
                    tenant_id=tenant_id,
                    version_id=version_id,
                    product_sku_id=sku_id,
                )
                publish_project_product_master_version(
                    connection,
                    tenant_id=tenant_id,
                    version_id=version_id,
                    actor_id="superadmin",
                )

            with pytest.raises(AuditCoreError) as exc_info:
                resolve_effective_project_product_master_version(
                    connection,
                    tenant_id=tenant_id,
                    effective_on=date(2026, 8, 20),
                )
            assert exc_info.value.error_code == "VAC-MASTER-003"
            assert exc_info.value.status_code == 409
    finally:
        engine.dispose()


def test_product_master_versions_are_isolated_by_project() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC02 Product Master integration tests")

    engine = create_engine(database_url)
    suffix_a = uuid4().hex
    suffix_b = uuid4().hex
    tenant_a = f"tenant-product-a-{suffix_a}"
    tenant_b = f"tenant-product-b-{suffix_b}"
    try:
        with engine.begin() as connection:
            sku_a = _seed_project_and_sku(connection, suffix=suffix_a, tenant_id=tenant_a)
            _seed_project_and_sku(connection, suffix=suffix_b, tenant_id=tenant_b)
            version_a = create_project_product_master_version(
                connection,
                tenant_id=tenant_a,
                effective_from=date(2026, 8, 1),
                actor_id="superadmin",
            )
            add_project_product_master_item(
                connection,
                tenant_id=tenant_a,
                version_id=version_a,
                product_sku_id=sku_a,
            )
            publish_project_product_master_version(
                connection,
                tenant_id=tenant_a,
                version_id=version_a,
                actor_id="superadmin",
            )

            with pytest.raises(AuditCoreError) as exc_info:
                resolve_effective_project_product_master_version(
                    connection,
                    tenant_id=tenant_b,
                    effective_on=date(2026, 8, 20),
                )
            assert exc_info.value.error_code == "VAC-MASTER-002"
    finally:
        engine.dispose()
