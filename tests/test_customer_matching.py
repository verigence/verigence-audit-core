import logging
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.customer_matching import (
    add_customer_match_key,
    protect_normalized_match_key,
)
from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal


def test_protected_match_finds_customers_across_dealers_without_raw_id_logging(caplog) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for customer matching integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-match-{suffix}"
    normalized_pan = "ABCDE1234F"
    match_hash = protect_normalized_match_key(normalized_pan, "test-match-secret")

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"MCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Match OEM') RETURNING oem_id"
            ),
            {"code": f"MOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Match Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"MP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )

        customer_ids = []
        dealer_ids = []
        for index in (1, 2):
            dealer_id = connection.execute(
                text(
                    "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                    "VALUES (:tenant_id, :code, :name) RETURNING dealer_id"
                ),
                {
                    "tenant_id": tenant_id,
                    "code": f"MD{index}-{suffix}",
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
                    "code": f"MO{index}-{suffix}",
                    "name": f"Outlet {index}",
                },
            ).scalar_one()
            customer_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.customers (
                        tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                    ) VALUES (
                        :tenant_id, :dealer_id, :outlet_id, 'RETAIL', :name
                    ) RETURNING customer_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                    "name": f"Customer {index}",
                },
            ).scalar_one()
            add_customer_match_key(
                connection,
                tenant_id=tenant_id,
                customer_id=customer_id,
                identity_type="PAN",
                match_hash=match_hash,
                source_kind="OPERATIONAL_INPUT",
            )
            customer_ids.append(customer_id)
            dealer_ids.append(dealer_id)

    def connection_override():
        with engine.begin() as connection:
            yield connection

    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject="match-user",
        tenant_id=tenant_id,
        permissions=(),
    )
    caplog.set_level(logging.INFO, logger="audit_core")
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(
            f"/v1/tenants/{tenant_id}/customers/matches",
            params={"identityType": "PAN", "matchHash": match_hash},
        )

        assert response.status_code == 200
        assert {item["customerId"] for item in response.json()} == {
            str(customer_ids[0]),
            str(customer_ids[1]),
        }
        assert {item["dealerId"] for item in response.json()} == {
            str(dealer_ids[0]),
            str(dealer_ids[1]),
        }
        recorded = " ".join(repr(record.__dict__) for record in caplog.records)
        assert normalized_pan not in recorded
        assert match_hash not in recorded
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
