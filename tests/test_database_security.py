import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from audit_core.db import set_tenant_context


@pytest.fixture
def database_engine():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for database security integration tests")
    engine = create_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_projects(connection):
    suffix = uuid4().hex
    category_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.product_categories (category_code, category_name)
            VALUES (:code, :name)
            RETURNING product_category_id
            """
        ),
        {"code": f"CAT-{suffix}", "name": f"Category {suffix}"},
    ).scalar_one()
    oem_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.oems (oem_code, oem_name)
            VALUES (:code, :name)
            RETURNING oem_id
            """
        ),
        {"code": f"OEM-{suffix}", "name": f"OEM {suffix}"},
    ).scalar_one()

    tenant_a = f"tenant-a-{suffix}"
    tenant_b = f"tenant-b-{suffix}"
    for tenant_id in (tenant_a, tenant_b):
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id,
                    project_code,
                    project_name,
                    oem_id,
                    product_category_id,
                    effective_start_date
                ) VALUES (
                    :tenant_id,
                    :project_code,
                    :project_name,
                    :oem_id,
                    :product_category_id,
                    CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_code": f"P-{tenant_id}",
                "project_name": f"Project {tenant_id}",
                "oem_id": oem_id,
                "product_category_id": category_id,
            },
        )

    return tenant_a, tenant_b


def test_runtime_role_is_non_owner_and_has_no_rls_bypass(database_engine) -> None:
    with database_engine.connect() as connection:
        properties = connection.execute(
            text(
                """
                SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls
                FROM pg_roles
                WHERE rolname = 'audit_core_runtime'
                """
            )
        ).one()
        owner = connection.execute(
            text(
                """
                SELECT tableowner
                FROM pg_tables
                WHERE schemaname = 'auditcore' AND tablename = 'projects'
                """
            )
        ).scalar_one()

    assert tuple(properties) == (False, False, False, False, False)
    assert owner != "audit_core_runtime"


def test_runtime_role_cannot_read_or_insert_cross_tenant(database_engine) -> None:
    with database_engine.begin() as owner_connection:
        tenant_a, tenant_b = _seed_projects(owner_connection)

    with database_engine.connect() as connection:
        with connection.begin():
            connection.execute(text("SET ROLE audit_core_runtime"))
            try:
                set_tenant_context(connection, tenant_a)
                visible_tenants = connection.execute(
                    text("SELECT tenant_id FROM auditcore.projects ORDER BY tenant_id")
                ).scalars().all()
                cross_tenant = connection.execute(
                    text(
                        "SELECT tenant_id FROM auditcore.projects WHERE tenant_id = :tenant_id"
                    ),
                    {"tenant_id": tenant_b},
                ).scalar_one_or_none()

                assert visible_tenants == [tenant_a]
                assert cross_tenant is None

                with pytest.raises(DBAPIError, match="row-level security"):
                    with connection.begin_nested():
                        connection.execute(
                            text(
                                """
                                INSERT INTO auditcore.business_status_codes (
                                    tenant_id, domain_key, status_code, status_label
                                ) VALUES (
                                    :tenant_id, 'JOURNEY', 'TEST', 'Test'
                                )
                                """
                            ),
                            {"tenant_id": tenant_b},
                        )
            finally:
                connection.execute(text("RESET ROLE"))


def test_runtime_role_has_no_delete_privilege(database_engine) -> None:
    with database_engine.begin() as owner_connection:
        tenant_a, _ = _seed_projects(owner_connection)
        can_delete = owner_connection.execute(
            text(
                "SELECT has_table_privilege('audit_core_runtime', 'auditcore.projects', 'DELETE')"
            )
        ).scalar_one()

    assert can_delete is False

    with database_engine.connect() as connection:
        with connection.begin():
            connection.execute(text("SET ROLE audit_core_runtime"))
            try:
                set_tenant_context(connection, tenant_a)
                with pytest.raises(DBAPIError, match="permission denied"):
                    with connection.begin_nested():
                        connection.execute(
                            text("DELETE FROM auditcore.projects WHERE tenant_id = :tenant_id"),
                            {"tenant_id": tenant_a},
                        )
            finally:
                connection.execute(text("RESET ROLE"))


def test_published_master_content_is_immutable(database_engine) -> None:
    with database_engine.begin() as connection:
        tenant_a, _ = _seed_projects(connection)
        policy_version_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.project_policy_versions (
                    tenant_id, version_no, effective_from, policy_settings
                ) VALUES (
                    :tenant_id, 1, CURRENT_DATE, '{"mode":"baseline"}'::jsonb
                )
                RETURNING policy_version_id
                """
            ),
            {"tenant_id": tenant_a},
        ).scalar_one()
        connection.execute(
            text(
                """
                UPDATE auditcore.project_policy_versions
                SET lifecycle_status = 'PUBLISHED', published_at_utc = now()
                WHERE tenant_id = :tenant_id AND policy_version_id = :policy_version_id
                """
            ),
            {"tenant_id": tenant_a, "policy_version_id": policy_version_id},
        )

        with pytest.raises(DBAPIError, match="published master version can only be retired"):
            with connection.begin_nested():
                connection.execute(
                    text(
                        """
                        UPDATE auditcore.project_policy_versions
                        SET policy_settings = '{"mode":"mutated"}'::jsonb
                        WHERE tenant_id = :tenant_id AND policy_version_id = :policy_version_id
                        """
                    ),
                    {"tenant_id": tenant_a, "policy_version_id": policy_version_id},
                )
