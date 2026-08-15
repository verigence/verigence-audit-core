from __future__ import annotations

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal


def test_commercials_preserve_standard_actual_provenance_and_master_versions() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for commercials integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-commercial-{suffix}"
    actor_id = f"pc-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"CCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Commercial OEM') RETURNING oem_id"
            ),
            {"code": f"COEM-{suffix}"},
        ).scalar_one()
        model_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
                "VALUES (:oem_id, 'MODEL', 'Model') RETURNING model_id"
            ),
            {"oem_id": oem_id},
        ).scalar_one()
        variant_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
                "VALUES (:model_id, 'VARIANT', 'Variant') RETURNING variant_id"
            ),
            {"model_id": model_id},
        ).scalar_one()
        sku_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_skus (
                    oem_id, model_id, variant_id, sku_code
                ) VALUES (
                    :oem_id, :model_id, :variant_id, :sku_code
                ) RETURNING product_sku_id
                """
            ),
            {
                "oem_id": oem_id,
                "model_id": model_id,
                "variant_id": variant_id,
                "sku_code": f"CSKU-{suffix}",
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Commercial Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"CP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Commercial Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"CD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Commercial Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"CO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Commercial Customer'
                ) RETURNING customer_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id, journey_reference
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'COMMERCIAL-JOURNEY'
                ) RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
                "customer_id": customer_id,
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_products (
                    tenant_id, journey_id, product_sku_id,
                    model_code_snapshot, model_name_snapshot,
                    variant_code_snapshot, variant_name_snapshot,
                    selection_source
                ) VALUES (
                    :tenant_id, :journey_id, :sku_id,
                    'MODEL', 'Model', 'VARIANT', 'Variant', 'OPERATIONAL_INPUT'
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "sku_id": sku_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code,
                    dealer_id, outlet_id
                ) VALUES (
                    :tenant_id, :actor_id, 'PC', :dealer_id, :outlet_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
            },
        )
        price_list_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.price_lists (
                    tenant_id, price_list_code, price_list_name
                ) VALUES (:tenant_id, :code, 'Commercial Price List')
                RETURNING price_list_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"PL-{suffix}"},
        ).scalar_one()
        price_version_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.price_list_versions (
                    tenant_id, price_list_id, version_no, lifecycle_status,
                    effective_from, currency_code, published_at_utc
                ) VALUES (
                    :tenant_id, :price_list_id, 1, 'PUBLISHED',
                    CURRENT_DATE, 'INR', now()
                ) RETURNING price_list_version_id
                """
            ),
            {"tenant_id": tenant_id, "price_list_id": price_list_id},
        ).scalar_one()
        price_item_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.price_list_items (
                    tenant_id, price_list_version_id, product_sku_id,
                    component_key, standard_amount
                ) VALUES (
                    :tenant_id, :version_id, :sku_id,
                    'EX_SHOWROOM', 1000000.00
                ) RETURNING price_list_item_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "version_id": price_version_id,
                "sku_id": sku_id,
            },
        ).scalar_one()
        scheme_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.discount_schemes (
                    tenant_id, scheme_code, scheme_name
                ) VALUES (:tenant_id, :code, 'Consumer Scheme')
                RETURNING discount_scheme_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"DS-{suffix}"},
        ).scalar_one()
        scheme_version_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.discount_scheme_versions (
                    tenant_id, discount_scheme_id, version_no,
                    lifecycle_status, effective_from, published_at_utc
                ) VALUES (
                    :tenant_id, :scheme_id, 1,
                    'PUBLISHED', CURRENT_DATE, now()
                ) RETURNING discount_scheme_version_id
                """
            ),
            {"tenant_id": tenant_id, "scheme_id": scheme_id},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.discount_scheme_benefits (
                    tenant_id, discount_scheme_version_id, benefit_key,
                    benefit_type, amount_value
                ) VALUES (
                    :tenant_id, :version_id, 'CONSUMER_OFFER', 'AMOUNT', 25000.00
                )
                """
            ),
            {"tenant_id": tenant_id, "version_id": scheme_version_id},
        )

    def connection_override():
        with engine.begin() as connection:
            yield connection

    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=actor_id,
        tenant_id=tenant_id,
        permissions=("audit.journey.read", "audit.journey.update"),
    )
    try:
        client = TestClient(app, raise_server_exceptions=False)
        url = f"/v1/tenants/{tenant_id}/journeys/{journey_id}/commercials"
        response = client.put(
            url,
            json={
                "lines": [
                    {
                        "componentKey": "EX_SHOWROOM",
                        "priceListItemId": str(price_item_id),
                        "actualAmount": "975000.00",
                        "actualSourceKind": "OPERATIONAL_INPUT",
                        "sourceReference": "dealer-booking-form",
                    }
                ],
                "discounts": [
                    {
                        "discountSchemeVersionId": str(scheme_version_id),
                        "discountKey": "CONSUMER_OFFER",
                        "actualDiscountAmount": "30000.00",
                        "actualSourceKind": "OPERATIONAL_INPUT",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        line = body["lines"][0]
        discount = body["discounts"][0]
        assert Decimal(str(line["standardAmount"])) == Decimal("1000000.00")
        assert Decimal(str(line["actualAmount"])) == Decimal("975000.00")
        assert line["priceListVersionId"] == str(price_version_id)
        assert line["actualSourceKind"] == "OPERATIONAL_INPUT"
        assert line["sourceReference"] == "dealer-booking-form"
        assert Decimal(str(discount["standardEligibleAmount"])) == Decimal("25000.00")
        assert Decimal(str(discount["actualDiscountAmount"])) == Decimal("30000.00")
        assert discount["discountSchemeVersionId"] == str(scheme_version_id)
        assert discount["actualSourceKind"] == "OPERATIONAL_INPUT"

        read_back = client.get(url)
        assert read_back.status_code == 200
        assert read_back.json() == body
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
