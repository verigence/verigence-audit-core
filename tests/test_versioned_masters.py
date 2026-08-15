import os
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from audit_core.versioned_masters import (
    add_document_requirement,
    create_audit_control,
    create_audit_control_version,
    create_document_profile,
    create_document_profile_version,
    create_project_policy_version,
    publish_master_version,
    retire_master_version,
)


def test_document_control_and_policy_versions_are_immutable_and_referenceable() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for versioned master integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-master-{suffix}"
    try:
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
                    "VALUES (:code, 'Master OEM') RETURNING oem_id"
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
                        :tenant_id, :code, 'Master Project', :oem_id,
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
            dealer_id = connection.execute(
                text(
                    "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                    "VALUES (:tenant_id, :code, 'Master Dealer') RETURNING dealer_id"
                ),
                {"tenant_id": tenant_id, "code": f"MD-{suffix}"},
            ).scalar_one()
            outlet_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.dealer_outlets (
                        tenant_id, dealer_id, outlet_code, outlet_name
                    ) VALUES (
                        :tenant_id, :dealer_id, :code, 'Master Outlet'
                    ) RETURNING outlet_id
                    """
                ),
                {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"MO-{suffix}"},
            ).scalar_one()
            customer_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.customers (
                        tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                    ) VALUES (
                        :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Master Customer'
                    ) RETURNING customer_id
                    """
                ),
                {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
            ).scalar_one()

            policy_version_id = create_project_policy_version(
                connection,
                tenant_id=tenant_id,
                version_no=1,
                effective_from=date(2026, 8, 1),
                actor_id="admin",
            )
            profile_id = create_document_profile(
                connection,
                tenant_id=tenant_id,
                code="VEHICLE-SALE",
                name="Vehicle Sale Documents",
                actor_id="admin",
            )
            profile_version_id = create_document_profile_version(
                connection,
                tenant_id=tenant_id,
                profile_id=profile_id,
                version_no=1,
                effective_from=date(2026, 8, 1),
                actor_id="admin",
            )
            requirement_id = add_document_requirement(
                connection,
                tenant_id=tenant_id,
                profile_version_id=profile_version_id,
                requirement_key="BOOKING_DOCKET",
                document_type_key="BOOKING_DOCKET",
                process_area="BOOKING",
                requirement_level="REQUIRED",
            )
            control_id = create_audit_control(
                connection,
                tenant_id=tenant_id,
                key="BOOKING_DOC_PRESENT",
                name="Booking document present",
                process_area="BOOKING",
                actor_id="admin",
            )
            control_version_id = create_audit_control_version(
                connection,
                tenant_id=tenant_id,
                audit_control_id=control_id,
                version_no=1,
                effective_from=date(2026, 8, 1),
                evaluator_key="booking_document_present",
                actor_id="admin",
            )

            for master_type, version_id in (
                ("POLICY", policy_version_id),
                ("DOCUMENT_PROFILE", profile_version_id),
                ("AUDIT_CONTROL", control_version_id),
            ):
                publish_master_version(
                    connection,
                    master_type=master_type,
                    tenant_id=tenant_id,
                    version_id=version_id,
                    actor_id="admin",
                )

            journey_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.journeys (
                        tenant_id, dealer_id, outlet_id, customer_id,
                        document_requirement_profile_version_id, policy_version_id
                    ) VALUES (
                        :tenant_id, :dealer_id, :outlet_id, :customer_id,
                        :profile_version_id, :policy_version_id
                    ) RETURNING journey_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                    "customer_id": customer_id,
                    "profile_version_id": profile_version_id,
                    "policy_version_id": policy_version_id,
                },
            ).scalar_one()
            references = connection.execute(
                text(
                    """
                    SELECT document_requirement_profile_version_id, policy_version_id
                    FROM auditcore.journeys
                    WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).one()
            assert tuple(references) == (profile_version_id, policy_version_id)

            with (
                pytest.raises(DBAPIError, match="published master version can only be retired"),
                connection.begin_nested(),
            ):
                connection.execute(
                    text(
                        "UPDATE auditcore.project_policy_versions "
                        "SET policy_settings = jsonb_build_object('changed', true) "
                        "WHERE tenant_id = :tenant_id AND policy_version_id = :version_id"
                    ),
                    {"tenant_id": tenant_id, "version_id": policy_version_id},
                )

            with (
                pytest.raises(DBAPIError, match="child rows may only be changed"),
                connection.begin_nested(),
            ):
                connection.execute(
                    text(
                        "UPDATE auditcore.document_requirement_items "
                        "SET document_type_key = 'CHANGED' "
                        "WHERE tenant_id = :tenant_id AND document_requirement_item_id = :item_id"
                    ),
                    {"tenant_id": tenant_id, "item_id": requirement_id},
                )

            with (
                pytest.raises(DBAPIError, match="published master version can only be retired"),
                connection.begin_nested(),
            ):
                connection.execute(
                    text(
                        "UPDATE auditcore.audit_control_versions "
                        "SET evaluator_key = 'changed' "
                        "WHERE tenant_id = :tenant_id AND audit_control_version_id = :version_id"
                    ),
                    {"tenant_id": tenant_id, "version_id": control_version_id},
                )

            for master_type, version_id in (
                ("POLICY", policy_version_id),
                ("DOCUMENT_PROFILE", profile_version_id),
                ("AUDIT_CONTROL", control_version_id),
            ):
                retire_master_version(
                    connection,
                    master_type=master_type,
                    tenant_id=tenant_id,
                    version_id=version_id,
                    actor_id="admin",
                )
    finally:
        engine.dispose()
