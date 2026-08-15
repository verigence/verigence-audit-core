import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.authorization import AuthorizationError
from audit_core.business_assignments import (
    create_business_assignment,
    require_business_scope,
)
from audit_core.security import Principal


def test_business_scope_allows_assigned_and_denies_unassigned() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for business assignment integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-scope-{suffix}"
    try:
        with engine.begin() as connection:
            category_id = connection.execute(
                text(
                    "INSERT INTO auditcore.product_categories (category_code, category_name) "
                    "VALUES (:code, :name) RETURNING product_category_id"
                ),
                {"code": f"ACAT-{suffix}", "name": f"Category {suffix}"},
            ).scalar_one()
            oem_id = connection.execute(
                text(
                    "INSERT INTO auditcore.oems (oem_code, oem_name) "
                    "VALUES (:code, :name) RETURNING oem_id"
                ),
                {"code": f"AOEM-{suffix}", "name": f"OEM {suffix}"},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.projects (
                        tenant_id, project_code, project_name, oem_id,
                        product_category_id, effective_start_date
                    ) VALUES (
                        :tenant_id, :code, 'Scope Test', :oem_id,
                        :category_id, CURRENT_DATE
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "code": f"AP-{suffix}",
                    "oem_id": oem_id,
                    "category_id": category_id,
                },
            )

            scopes = []
            for index in (1, 2):
                dealer_id = connection.execute(
                    text(
                        """
                        INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                        VALUES (:tenant_id, :code, :name) RETURNING dealer_id
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "code": f"AD{index}-{suffix}",
                        "name": f"Dealer {index}",
                    },
                ).scalar_one()
                outlet_id = connection.execute(
                    text(
                        """
                        INSERT INTO auditcore.dealer_outlets (
                            tenant_id, dealer_id, outlet_code, outlet_name
                        ) VALUES (
                            :tenant_id, :dealer_id, :code, :name
                        ) RETURNING outlet_id
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "dealer_id": dealer_id,
                        "code": f"AO{index}-{suffix}",
                        "name": f"Outlet {index}",
                    },
                ).scalar_one()
                scopes.append((dealer_id, outlet_id))

            assigned_dealer, assigned_outlet = scopes[0]
            other_dealer, other_outlet = scopes[1]
            create_business_assignment(
                connection,
                tenant_id=tenant_id,
                security_actor_id="pc-user",
                business_role_code="PC",
                dealer_id=assigned_dealer,
                outlet_id=assigned_outlet,
                created_by_actor_id="admin-user",
            )
            principal = Principal(
                subject="pc-user",
                tenant_id=tenant_id,
                permissions=(),
            )

            require_business_scope(
                connection,
                principal,
                tenant_id=tenant_id,
                dealer_id=assigned_dealer,
                outlet_id=assigned_outlet,
            )

            with pytest.raises(AuthorizationError) as exc_info:
                require_business_scope(
                    connection,
                    principal,
                    tenant_id=tenant_id,
                    dealer_id=other_dealer,
                    outlet_id=other_outlet,
                )
            assert exc_info.value.error_code == "VAC-AUTH-004"
    finally:
        engine.dispose()
