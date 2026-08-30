import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from audit_core import mahindra_masters
from audit_core.discount_policy import resolve_numeric_policy_parameter
from audit_core.mahindra_dealer_policy_scope import install_dealer_policy_scope


def _seed_project(connection, *, tenant_id: str, suffix: str):
    category_id = connection.execute(
        text(
            "INSERT INTO auditcore.product_categories (category_code, category_name) "
            "VALUES (:code, :name) RETURNING product_category_id"
        ),
        {"code": f"POL-CAT-{suffix}", "name": f"Policy Category {suffix}"},
    ).scalar_one()
    oem_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.oems (oem_code, oem_name)
            VALUES (:code, :name)
            RETURNING oem_id
            """
        ),
        {"code": f"POL-OEM-{suffix}", "name": f"Policy OEM {suffix}"},
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
            "code": f"POL-PROJ-{suffix}",
            "name": f"Policy Project {suffix}",
            "oem_id": oem_id,
            "category_id": category_id,
        },
    )
    dealer_ids = {}
    for label in ("A", "B"):
        dealer_ids[label] = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (
                    tenant_id, dealer_code, dealer_name
                ) VALUES (:tenant_id, :code, :name)
                RETURNING dealer_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"DEALER-{label}-{suffix}",
                "name": f"Dealer {label} {suffix}",
            },
        ).scalar_one()
    return dealer_ids


def _publish_policy(connection, *, tenant_id: str, dealer_a_id):
    version_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.discount_policy_versions (
                tenant_id, version_no, effective_from, lifecycle_status,
                created_by_actor_id
            ) VALUES (:tenant_id, 1, DATE '2026-08-01', 'DRAFT', 'test-admin')
            RETURNING discount_policy_version_id
            """
        ),
        {"tenant_id": tenant_id},
    ).scalar_one()
    rows = [
        ("PROJECT", None, "MINIMUM_BOOKING_AMOUNT", Decimal(21000), "INR"),
        (
            "PROJECT",
            None,
            "MR_MAX_PERCENT_PREVIOUS_MONTH_RETAIL",
            Decimal(5),
            "PERCENT",
        ),
        (
            "DEALER",
            dealer_a_id,
            "MR_MAX_PERCENT_PREVIOUS_MONTH_RETAIL",
            Decimal(7),
            "PERCENT",
        ),
    ]
    for scope_type, dealer_id, key, value, unit in rows:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.discount_policy_parameters (
                    tenant_id, discount_policy_version_id, scope_type,
                    dealer_id, parameter_key, value_type, value_number, unit
                ) VALUES (
                    :tenant_id, :version_id, :scope_type,
                    :dealer_id, :key, 'NUMBER', :value, :unit
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "version_id": version_id,
                "scope_type": scope_type,
                "dealer_id": dealer_id,
                "key": key,
                "value": value,
                "unit": unit,
            },
        )
    connection.execute(
        text(
            """
            UPDATE auditcore.discount_policy_versions
            SET lifecycle_status='PUBLISHED',
                published_by_actor_id='test-admin',
                published_at_utc=now(), updated_at_utc=now()
            WHERE tenant_id=:tenant_id
              AND discount_policy_version_id=:version_id
            """
        ),
        {"tenant_id": tenant_id, "version_id": version_id},
    )
    return version_id


def test_dealer_scope_is_part_of_admin_workbook_contract() -> None:
    install_dealer_policy_scope(mahindra_masters)
    assert "dealer_code" in mahindra_masters._POLICY_COLUMNS
    assert "DEALER" in mahindra_masters._POLICY_SCOPE_TYPES


def test_project_minimum_and_dealer_mr_override_resolve_by_business_date() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for Dealer policy integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-dealer-policy-{suffix}"
    try:
        with engine.begin() as connection:
            dealers = _seed_project(connection, tenant_id=tenant_id, suffix=suffix)
            _publish_policy(
                connection,
                tenant_id=tenant_id,
                dealer_a_id=dealers["A"],
            )

            booking_minimum = resolve_numeric_policy_parameter(
                connection,
                tenant_id=tenant_id,
                parameter_key="MINIMUM_BOOKING_AMOUNT",
                effective_on=date(2026, 8, 30),
            )
            assert booking_minimum is not None
            assert booking_minimum["scope_type"] == "PROJECT"
            assert booking_minimum["value_number"] == Decimal(21000)
            assert booking_minimum["unit"] == "INR"

            dealer_a_mr = resolve_numeric_policy_parameter(
                connection,
                tenant_id=tenant_id,
                parameter_key="MR_MAX_PERCENT_PREVIOUS_MONTH_RETAIL",
                effective_on=date(2026, 8, 30),
                dealer_id=dealers["A"],
            )
            assert dealer_a_mr is not None
            assert dealer_a_mr["scope_type"] == "DEALER"
            assert dealer_a_mr["value_number"] == Decimal(7)

            dealer_b_mr = resolve_numeric_policy_parameter(
                connection,
                tenant_id=tenant_id,
                parameter_key="MR_MAX_PERCENT_PREVIOUS_MONTH_RETAIL",
                effective_on=date(2026, 8, 30),
                dealer_id=dealers["B"],
            )
            assert dealer_b_mr is not None
            assert dealer_b_mr["scope_type"] == "PROJECT"
            assert dealer_b_mr["value_number"] == Decimal(5)
    finally:
        engine.dispose()


def test_dealer_policy_reference_cannot_cross_tenant_boundary() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for Dealer policy integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_a = f"tenant-policy-a-{suffix}"
    tenant_b = f"tenant-policy-b-{suffix}"
    try:
        with engine.begin() as connection:
            _seed_project(connection, tenant_id=tenant_a, suffix=f"a-{suffix}")
            dealers_b = _seed_project(connection, tenant_id=tenant_b, suffix=f"b-{suffix}")
            draft_version_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.discount_policy_versions (
                        tenant_id, version_no, effective_from, lifecycle_status,
                        created_by_actor_id
                    ) VALUES (
                        :tenant_id, 1, DATE '2026-08-01', 'DRAFT', 'test-admin'
                    ) RETURNING discount_policy_version_id
                    """
                ),
                {"tenant_id": tenant_a},
            ).scalar_one()

            with (
                pytest.raises(DBAPIError),
                connection.begin_nested(),
            ):
                connection.execute(
                    text(
                        """
                        INSERT INTO auditcore.discount_policy_parameters (
                            tenant_id, discount_policy_version_id, scope_type,
                            dealer_id, parameter_key, value_type, value_number, unit
                        ) VALUES (
                            :tenant_id, :version_id, 'DEALER',
                            :dealer_id, 'CROSS_TENANT_TEST', 'NUMBER', 1, 'COUNT'
                        )
                        """
                    ),
                    {
                        "tenant_id": tenant_a,
                        "version_id": draft_version_id,
                        "dealer_id": dealers_b["A"],
                    },
                )
    finally:
        engine.dispose()
