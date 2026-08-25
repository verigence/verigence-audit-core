from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def identity_date_db_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 identity/date integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-identity-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, 'Vehicle')
                RETURNING product_category_id
                """
            ),
            {"code": f"ID-CAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, 'Identity OEM')
                RETURNING oem_id
                """
            ),
            {"code": f"ID-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :code, 'Identity Project', :oem_id,
                    :category_id, CURRENT_DATE - 1,
                    'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"ID-P-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Identity Dealer')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"ID-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Identity Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"ID-O-{suffix}"},
        ).scalar_one()

        standalone_customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'INDIVIDUAL', 'Standalone Name'
                ) RETURNING customer_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
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
                    tenant_id, dealer_id, outlet_id, customer_id, journey_reference
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, :reference
                ) RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
                "customer_id": customer_id,
                "reference": f"ID-J-{suffix}",
            },
        ).scalar_one()

    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "customer_id": customer_id,
            "standalone_customer_id": standalone_customer_id,
            "journey_id": journey_id,
        }
    finally:
        engine.dispose()


def _insert_identity_evidence(connection, setup, *, document_type: str):
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.evidence (
                tenant_id, journey_id, customer_id,
                di_subject_id, di_document_id,
                document_type_key, evidence_purpose
            ) VALUES (
                :tenant_id, :journey_id, :customer_id,
                :di_subject_id, :di_document_id,
                :document_type, 'UC03_IDENTITY'
            ) RETURNING evidence_id
            """
        ),
        {
            "tenant_id": setup["tenant_id"],
            "journey_id": setup["journey_id"],
            "customer_id": setup["customer_id"],
            "di_subject_id": uuid4(),
            "di_document_id": uuid4(),
            "document_type": document_type,
        },
    ).scalar_one()


def _accept_identity_proposal(
    connection,
    setup,
    *,
    evidence_id,
    document_type: str,
    field_key: str,
    name: str,
):
    proposal_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_capture_proposals (
                tenant_id, journey_id, stage_code, field_key,
                source_evidence_id, source_evidence_fact_id,
                source_document_type_key, proposed_value
            ) VALUES (
                :tenant_id, :journey_id, 'BOOKING', :field_key,
                :evidence_id, :fact_id, :document_type, CAST(:proposed_value AS jsonb)
            ) RETURNING capture_proposal_id
            """
        ),
        {
            "tenant_id": setup["tenant_id"],
            "journey_id": setup["journey_id"],
            "field_key": field_key,
            "evidence_id": evidence_id,
            "fact_id": f"fact-{uuid4().hex}",
            "document_type": document_type,
            "proposed_value": json.dumps(name),
        },
    ).scalar_one()
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_capture_proposals
            SET proposal_status = 'ACCEPTED',
                accepted_value = CAST(:accepted_value AS jsonb),
                accepted_by_actor_id = 'pc-identity-test',
                accepted_by_role = 'PC',
                accepted_at_utc = now()
            WHERE tenant_id = :tenant_id
              AND capture_proposal_id = :proposal_id
            """
        ),
        {
            "tenant_id": setup["tenant_id"],
            "proposal_id": proposal_id,
            "accepted_value": json.dumps(name),
        },
    )


def test_entered_name_is_mutable_before_journey_but_immutable_after(identity_date_db_setup) -> None:
    setup = identity_date_db_setup
    with setup["engine"].begin() as connection:
        connection.execute(
            text(
                """
                UPDATE auditcore.customers
                SET display_name = 'Standalone Updated'
                WHERE tenant_id = :tenant_id AND customer_id = :customer_id
                """
            ),
            {
                "tenant_id": setup["tenant_id"],
                "customer_id": setup["standalone_customer_id"],
            },
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.customers
                SET display_name = 'Attempted Journey Name Change'
                WHERE tenant_id = :tenant_id AND customer_id = :customer_id
                """
            ),
            {"tenant_id": setup["tenant_id"], "customer_id": setup["customer_id"]},
        )
        rows = connection.execute(
            text(
                """
                SELECT customer_id, display_name
                FROM auditcore.customers
                WHERE tenant_id = :tenant_id
                  AND customer_id IN (:standalone_customer_id, :journey_customer_id)
                """
            ),
            {
                "tenant_id": setup["tenant_id"],
                "standalone_customer_id": setup["standalone_customer_id"],
                "journey_customer_id": setup["customer_id"],
            },
        ).mappings().all()
    names = {row["customer_id"]: row["display_name"] for row in rows}
    assert names[setup["standalone_customer_id"]] == "Standalone Updated"
    assert names[setup["customer_id"]] == "Entered Name"


def test_pan_and_aadhaar_update_legal_name_without_overwriting_entered_name(identity_date_db_setup) -> None:
    setup = identity_date_db_setup
    with setup["engine"].begin() as connection:
        pan_evidence_id = _insert_identity_evidence(connection, setup, document_type="pan_card")
        _accept_identity_proposal(
            connection,
            setup,
            evidence_id=pan_evidence_id,
            document_type="pan_card",
            field_key="pan_name",
            name="Ankit Kumar Ojha",
        )
        customer = connection.execute(
            text(
                """
                SELECT display_name, legal_name, legal_name_status,
                       legal_name_source_evidence_id
                FROM auditcore.customers
                WHERE tenant_id = :tenant_id AND customer_id = :customer_id
                """
            ),
            {"tenant_id": setup["tenant_id"], "customer_id": setup["customer_id"]},
        ).mappings().one()

    assert customer["display_name"] == "Entered Name"
    assert customer["legal_name"] == "Ankit Kumar Ojha"
    assert customer["legal_name_status"] == "VERIFIED"
    assert customer["legal_name_source_evidence_id"] == pan_evidence_id

    with setup["engine"].begin() as connection:
        aadhaar_evidence_id = _insert_identity_evidence(connection, setup, document_type="aadhaar")
        _accept_identity_proposal(
            connection,
            setup,
            evidence_id=aadhaar_evidence_id,
            document_type="aadhaar",
            field_key="aadhaar_name",
            name="Ankit K Ojha",
        )
        conflicted = connection.execute(
            text(
                """
                SELECT display_name, legal_name, legal_name_status,
                       legal_name_source_evidence_id
                FROM auditcore.customers
                WHERE tenant_id = :tenant_id AND customer_id = :customer_id
                """
            ),
            {"tenant_id": setup["tenant_id"], "customer_id": setup["customer_id"]},
        ).mappings().one()

    assert conflicted["display_name"] == "Entered Name"
    assert conflicted["legal_name"] == "Ankit Kumar Ojha"
    assert conflicted["legal_name_status"] == "CONFLICT"
    assert conflicted["legal_name_source_evidence_id"] == pan_evidence_id


def test_actual_booking_date_allows_delayed_capture_but_rejects_future_date(identity_date_db_setup) -> None:
    setup = identity_date_db_setup
    with setup["engine"].begin() as connection:
        historical_date = connection.execute(
            text("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date - 1")
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date)
                VALUES (:tenant_id, :journey_id, :booking_date)
                """
            ),
            {
                "tenant_id": setup["tenant_id"],
                "journey_id": setup["journey_id"],
                "booking_date": historical_date,
            },
        )

    with pytest.raises(DBAPIError):
        with setup["engine"].begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE auditcore.bookings
                    SET booking_date = (now() AT TIME ZONE 'Asia/Kolkata')::date + 1
                    WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                    """
                ),
                {"tenant_id": setup["tenant_id"], "journey_id": setup["journey_id"]},
            )

    with setup["engine"].begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT b.booking_date,
                       ((j.created_at_utc AT TIME ZONE p.timezone_name)::date - b.booking_date)
                           AS capture_lag_days
                FROM auditcore.bookings b
                JOIN auditcore.journeys j
                  ON j.tenant_id = b.tenant_id AND j.journey_id = b.journey_id
                JOIN auditcore.projects p ON p.tenant_id = j.tenant_id
                WHERE b.tenant_id = :tenant_id AND b.journey_id = :journey_id
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": setup["journey_id"]},
        ).mappings().one()
    assert row["booking_date"] == historical_date
    assert row["capture_lag_days"] >= 1
