from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.uc03_identity_business_date import (
    _business_date_aware_completion_summary,
    _identity_aware_write_typed_capture,
)


@pytest.fixture
def historical_booking_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 business-date integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-history-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, 'Vehicle')
                RETURNING product_category_id
                """
            ),
            {"code": f"HIST-CAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, 'Historical OEM')
                RETURNING oem_id
                """
            ),
            {"code": f"HIST-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :code, 'Historical Booking Project', :oem_id,
                    :category_id, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"HIST-P-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        historical_date = connection.execute(text("SELECT CURRENT_DATE - 1")).scalar_one()

        historical_profile = connection.execute(
            text(
                """
                SELECT document_requirement_profile_id,
                       document_requirement_profile_version_id,
                       version_no
                FROM auditcore.document_requirement_profile_versions
                WHERE tenant_id = :tenant_id
                  AND lifecycle_status = 'PUBLISHED'
                  AND effective_from <= :historical_date
                ORDER BY effective_from DESC, version_no DESC
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id, "historical_date": historical_date},
        ).mappings().one()
        current_profile_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.document_requirement_profile_versions (
                    tenant_id, document_requirement_profile_id, version_no,
                    effective_from, lifecycle_status, created_by_actor_id
                ) VALUES (
                    :tenant_id, :profile_id, :version_no,
                    CURRENT_DATE, 'DRAFT', 'test.business-date'
                )
                RETURNING document_requirement_profile_version_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "profile_id": historical_profile["document_requirement_profile_id"],
                "version_no": int(historical_profile["version_no"]) + 1,
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.document_requirement_items (
                    tenant_id, document_requirement_profile_version_id,
                    requirement_key, document_type_key, process_area,
                    requirement_level, condition_config, sort_order
                )
                SELECT tenant_id, :new_version_id,
                       requirement_key, document_type_key, process_area,
                       requirement_level, condition_config, sort_order
                FROM auditcore.document_requirement_items
                WHERE tenant_id = :tenant_id
                  AND document_requirement_profile_version_id = :old_version_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "new_version_id": current_profile_id,
                "old_version_id": historical_profile[
                    "document_requirement_profile_version_id"
                ],
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.document_requirement_items (
                    tenant_id, document_requirement_profile_version_id,
                    requirement_key, document_type_key, process_area,
                    requirement_level, condition_config, sort_order
                ) VALUES (
                    :tenant_id, :version_id,
                    'current_day_only_document', 'current_day_only_document',
                    'BOOKING', 'OPTIONAL', '{}'::jsonb, 999
                )
                """
            ),
            {"tenant_id": tenant_id, "version_id": current_profile_id},
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.document_requirement_profile_versions
                SET lifecycle_status = 'PUBLISHED',
                    published_by_actor_id = 'test.business-date',
                    published_at_utc = now(), updated_at_utc = now()
                WHERE tenant_id = :tenant_id
                  AND document_requirement_profile_version_id = :version_id
                """
            ),
            {"tenant_id": tenant_id, "version_id": current_profile_id},
        )

        historical_policy_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.project_policy_versions (
                    tenant_id, version_no, lifecycle_status, effective_from,
                    created_by_actor_id, published_by_actor_id, published_at_utc
                ) VALUES (
                    :tenant_id, 1, 'PUBLISHED', CURRENT_DATE - 1,
                    'test.business-date', 'test.business-date', now()
                ) RETURNING policy_version_id
                """
            ),
            {"tenant_id": tenant_id},
        ).scalar_one()
        current_policy_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.project_policy_versions (
                    tenant_id, version_no, lifecycle_status, effective_from,
                    created_by_actor_id, published_by_actor_id, published_at_utc
                ) VALUES (
                    :tenant_id, 2, 'PUBLISHED', CURRENT_DATE,
                    'test.business-date', 'test.business-date', now()
                ) RETURNING policy_version_id
                """
            ),
            {"tenant_id": tenant_id},
        ).scalar_one()

        price_list_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.price_lists (
                    tenant_id, price_list_code, price_list_name, created_by_actor_id
                ) VALUES (
                    :tenant_id, :code, 'Historical Price List', 'test.business-date'
                ) RETURNING price_list_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"HIST-PRICE-{suffix}"},
        ).scalar_one()
        historical_price_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.price_list_versions (
                    tenant_id, price_list_id, version_no, lifecycle_status,
                    effective_from, created_by_actor_id,
                    published_by_actor_id, published_at_utc
                ) VALUES (
                    :tenant_id, :price_list_id, 1, 'PUBLISHED',
                    CURRENT_DATE - 1, 'test.business-date',
                    'test.business-date', now()
                ) RETURNING price_list_version_id
                """
            ),
            {"tenant_id": tenant_id, "price_list_id": price_list_id},
        ).scalar_one()
        current_price_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.price_list_versions (
                    tenant_id, price_list_id, version_no, lifecycle_status,
                    effective_from, created_by_actor_id,
                    published_by_actor_id, published_at_utc
                ) VALUES (
                    :tenant_id, :price_list_id, 2, 'PUBLISHED',
                    CURRENT_DATE, 'test.business-date',
                    'test.business-date', now()
                ) RETURNING price_list_version_id
                """
            ),
            {"tenant_id": tenant_id, "price_list_id": price_list_id},
        ).scalar_one()

        dealer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Historical Dealer')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"HIST-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Historical Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"HIST-O-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'INDIVIDUAL', 'Entered Name'
                ) RETURNING customer_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id,
                    document_requirement_profile_version_id,
                    policy_version_id, price_list_version_id,
                    journey_reference
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, :customer_id,
                    :profile_version_id, :policy_version_id, :price_version_id,
                    :reference
                ) RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
                "customer_id": customer_id,
                "profile_version_id": current_profile_id,
                "policy_version_id": current_policy_id,
                "price_version_id": current_price_id,
                "reference": f"HIST-J-{suffix}",
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_stage_states (
                    tenant_id, journey_id, stage_code, business_status,
                    audit_state, audit_status, first_started_at_utc,
                    latest_activity_at_utc, version_no
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING', 'BOOKING_STARTED',
                    'NOT_STARTED', 'NOT_EVALUATED', now(), now(), 1
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_requirements (
                    tenant_id, journey_id, document_requirement_item_id,
                    requirement_key, document_type_key, process_area,
                    requirement_level, requirement_status, condition_snapshot
                )
                SELECT CAST(:tenant_id AS varchar), CAST(:journey_id AS uuid),
                       i.document_requirement_item_id,
                       i.requirement_key, i.document_type_key, i.process_area,
                       i.requirement_level, 'PENDING', i.condition_config
                FROM auditcore.document_requirement_items i
                WHERE i.tenant_id = CAST(:tenant_id AS varchar)
                  AND i.document_requirement_profile_version_id = :profile_version_id
                  AND upper(i.process_area) = 'BOOKING'
                ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "profile_version_id": current_profile_id,
            },
        )
        current_only_requirement_id = connection.execute(
            text(
                """
                SELECT journey_document_requirement_id
                FROM auditcore.journey_document_requirements
                WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                  AND requirement_key = 'current_day_only_document'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
        current_only_evidence_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id,
                    journey_document_requirement_id,
                    di_subject_id, di_document_id,
                    document_type_key, evidence_purpose
                ) VALUES (
                    :tenant_id, :journey_id, :customer_id,
                    :requirement_id, :di_subject_id, :di_document_id,
                    'current_day_only_document', 'UC03_BOOKING'
                ) RETURNING evidence_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "customer_id": customer_id,
                "requirement_id": current_only_requirement_id,
                "di_subject_id": uuid4(),
                "di_document_id": uuid4(),
            },
        ).scalar_one()

    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "historical_date": historical_date,
            "historical_profile_id": historical_profile[
                "document_requirement_profile_version_id"
            ],
            "current_profile_id": current_profile_id,
            "historical_policy_id": historical_policy_id,
            "current_policy_id": current_policy_id,
            "historical_price_id": historical_price_id,
            "current_price_id": current_price_id,
            "current_only_requirement_id": current_only_requirement_id,
            "current_only_evidence_id": current_only_evidence_id,
        }
    finally:
        engine.dispose()


def test_actual_booking_date_rebinds_versions_without_deleting_evidence(
    historical_booking_setup,
) -> None:
    setup = historical_booking_setup
    with setup["engine"].begin() as connection:
        before = _business_date_aware_completion_summary(
            connection, setup["tenant_id"], setup["journey_id"]
        )
        assert "ACTUAL_BOOKING_DATE_REQUIRED" in {
            blocker["code"] for blocker in before["blockers"]
        }

        _identity_aware_write_typed_capture(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            field_key="BOOKING_DATE",
            value=setup["historical_date"].isoformat(),
            source_evidence_id=None,
        )
        uc03_codes = {
            blocker["code"]
            for blocker in _business_date_aware_completion_summary(
                connection, setup["tenant_id"], setup["journey_id"]
            )["blockers"]
        }

        journey = connection.execute(
            text(
                """
                SELECT document_requirement_profile_version_id,
                       policy_version_id, price_list_version_id
                FROM auditcore.journeys
                WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": setup["journey_id"]},
        ).mappings().one()
        current_only = connection.execute(
            text(
                """
                SELECT requirement_status, condition_snapshot
                FROM auditcore.journey_document_requirements
                WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                  AND requirement_key = 'current_day_only_document'
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": setup["journey_id"]},
        ).mappings().one()
        evidence = connection.execute(
            text(
                """
                SELECT association_status, journey_document_requirement_id
                FROM auditcore.evidence
                WHERE tenant_id = :tenant_id AND evidence_id = :evidence_id
                """
            ),
            {
                "tenant_id": setup["tenant_id"],
                "evidence_id": setup["current_only_evidence_id"],
            },
        ).mappings().one()
        booking_docket_item = connection.execute(
            text(
                """
                SELECT jdr.document_requirement_item_id
                FROM auditcore.journey_document_requirements jdr
                WHERE jdr.tenant_id = :tenant_id
                  AND jdr.journey_id = :journey_id
                  AND jdr.requirement_key = 'booking_docket'
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": setup["journey_id"]},
        ).scalar_one()
        historical_booking_docket_item = connection.execute(
            text(
                """
                SELECT document_requirement_item_id
                FROM auditcore.document_requirement_items
                WHERE tenant_id = :tenant_id
                  AND document_requirement_profile_version_id = :profile_version_id
                  AND requirement_key = 'booking_docket'
                """
            ),
            {
                "tenant_id": setup["tenant_id"],
                "profile_version_id": setup["historical_profile_id"],
            },
        ).scalar_one()

    assert journey["document_requirement_profile_version_id"] == setup[
        "historical_profile_id"
    ]
    assert journey["policy_version_id"] == setup["historical_policy_id"]
    assert journey["price_list_version_id"] == setup["historical_price_id"]
    assert current_only["requirement_status"] == "NOT_APPLICABLE"
    assert current_only["condition_snapshot"]["profileReconciliationState"] == (
        "SUPERSEDED_BY_ACTUAL_BOOKING_DATE"
    )
    assert evidence["association_status"] == "ACTIVE"
    assert evidence["journey_document_requirement_id"] == setup[
        "current_only_requirement_id"
    ]
    assert booking_docket_item == historical_booking_docket_item
    assert "ACTUAL_BOOKING_DATE_REQUIRED" not in uc03_codes
    assert "BUSINESS_DATE_CONFIGURATION_MISSING" not in uc03_codes
    assert "BUSINESS_DATE_CONFIGURATION_RECONCILIATION_PENDING" not in uc03_codes
