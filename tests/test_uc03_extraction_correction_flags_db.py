from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text


@pytest.fixture
def correction_flag_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 correction Flag integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-correction-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, 'Vehicle')
                RETURNING product_category_id
                """
            ),
            {"code": f"CORR-CAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, 'Correction OEM')
                RETURNING oem_id
                """
            ),
            {"code": f"CORR-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :code, 'Correction Project', :oem_id,
                    :category_id, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"CORR-P-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Correction Dealer')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"CORR-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (:tenant_id, :dealer_id, :code, 'Correction Outlet')
                RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"CORR-O-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'INDIVIDUAL', 'Entered Customer'
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
                "reference": f"CORR-J-{suffix}",
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
                    :tenant_id, :journey_id, 'BOOKING', 'BOOKING_IN_PROGRESS',
                    'IN_PROGRESS', 'NOT_EVALUATED', now(), now(), 1
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        )
        evidence_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id,
                    di_subject_id, di_document_id,
                    document_type_key, evidence_purpose
                ) VALUES (
                    :tenant_id, :journey_id, :customer_id,
                    :di_subject_id, :di_document_id,
                    'booking_docket', 'UC03_BOOKING:BOOKING_DOCKET'
                ) RETURNING evidence_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "customer_id": customer_id,
                "di_subject_id": uuid4(),
                "di_document_id": uuid4(),
            },
        ).scalar_one()

    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evidence_id": evidence_id,
        }
    finally:
        engine.dispose()


def _insert_proposal(connection, setup, *, field_key: str, machine_value: str, confidence: float):
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_capture_proposals (
                tenant_id, journey_id, stage_code, field_key,
                source_evidence_id, source_evidence_fact_id,
                source_document_type_key, proposed_value, confidence_score
            ) VALUES (
                :tenant_id, :journey_id, 'BOOKING', :field_key,
                :evidence_id, :fact_id, 'booking_docket',
                CAST(:proposed_value AS jsonb), :confidence
            ) RETURNING capture_proposal_id
            """
        ),
        {
            "tenant_id": setup["tenant_id"],
            "journey_id": setup["journey_id"],
            "field_key": field_key,
            "evidence_id": setup["evidence_id"],
            "fact_id": f"fact-{uuid4().hex}",
            "proposed_value": json.dumps({"value": machine_value}),
            "confidence": confidence,
        },
    ).scalar_one()


def _correct_proposal(connection, setup, proposal_id, *, corrected_value: str) -> None:
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_capture_proposals
            SET proposal_status='CORRECTED',
                accepted_value=CAST(:accepted_value AS jsonb),
                accepted_by_actor_id='pc-correction-test',
                accepted_by_role='PC',
                accepted_at_utc=now(),
                owning_domain_key='BOOKING',
                owning_record_reference='booking-test'
            WHERE tenant_id=:tenant_id
              AND capture_proposal_id=:proposal_id
            """
        ),
        {
            "tenant_id": setup["tenant_id"],
            "proposal_id": proposal_id,
            "accepted_value": json.dumps({"value": corrected_value}),
        },
    )


def _correction_flag(connection, setup, proposal_id):
    return connection.execute(
        text(
            """
            SELECT f.audit_finding_id, f.severity, f.title, f.description,
                   f.expected_summary, f.observed_summary, f.rule_key,
                   f.blocking_completion, f.origin_actor_id, f.origin_role_snapshot,
                   e.safe_payload
            FROM auditcore.audit_findings f
            JOIN auditcore.audit_finding_events e
              ON e.tenant_id=f.tenant_id
             AND e.audit_finding_id=f.audit_finding_id
             AND e.event_type='RAISED'
            WHERE f.tenant_id=:tenant_id
              AND f.journey_id=:journey_id
              AND f.rule_key='UC03_DI_CORRECTION_CONFIDENCE'
              AND e.safe_payload->>'proposalId'=:proposal_id
            """
        ),
        {
            "tenant_id": setup["tenant_id"],
            "journey_id": setup["journey_id"],
            "proposal_id": str(proposal_id),
        },
    ).mappings().one()


def test_correction_below_90_writes_info_flag_with_from_to(correction_flag_setup) -> None:
    setup = correction_flag_setup
    with setup["engine"].begin() as connection:
        proposal_id = _insert_proposal(
            connection,
            setup,
            field_key="booking_reference_number",
            machine_value="BK-1001",
            confidence=0.89,
        )
        _correct_proposal(connection, setup, proposal_id, corrected_value="BK-1007")
        flag = _correction_flag(connection, setup, proposal_id)

        assert flag["severity"] == "INFO"
        assert flag["title"] == "DI value corrected by PC"
        assert 'BK-1001' in flag["expected_summary"]
        assert 'BK-1007' in flag["observed_summary"]
        assert "89%" in flag["description"]
        assert flag["blocking_completion"] is False
        assert flag["origin_actor_id"] == "pc-correction-test"
        assert flag["origin_role_snapshot"] == "PC"
        assert "BK-1001" not in json.dumps(flag["safe_payload"])
        assert "BK-1007" not in json.dumps(flag["safe_payload"])

        linked = connection.execute(
            text(
                """
                SELECT count(*)
                FROM auditcore.finding_evidence
                WHERE tenant_id=:tenant_id
                  AND audit_finding_id=:flag_id
                  AND evidence_id=:evidence_id
                  AND linkage_purpose='CORRECTION_SOURCE'
                """
            ),
            {
                "tenant_id": setup["tenant_id"],
                "flag_id": flag["audit_finding_id"],
                "evidence_id": setup["evidence_id"],
            },
        ).scalar_one()
        assert linked == 1

        audit_status = connection.execute(
            text(
                """
                SELECT audit_status
                FROM auditcore.journey_stage_states
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": setup["journey_id"]},
        ).scalar_one()
        assert audit_status == "FLAGS_RAISED"


def test_correction_at_90_writes_high_flag_for_tl_review(correction_flag_setup) -> None:
    setup = correction_flag_setup
    with setup["engine"].begin() as connection:
        proposal_id = _insert_proposal(
            connection,
            setup,
            field_key="booking_reference_number",
            machine_value="BK-2001",
            confidence=0.90,
        )
        _correct_proposal(connection, setup, proposal_id, corrected_value="BK-2002")
        flag = _correction_flag(connection, setup, proposal_id)

        assert flag["severity"] == "HIGH"
        assert flag["title"] == "High-confidence DI value corrected — TL review required"
        assert "90%" in flag["description"]
        assert flag["blocking_completion"] is False


def test_accepted_value_without_correction_does_not_raise_correction_flag(
    correction_flag_setup,
) -> None:
    setup = correction_flag_setup
    with setup["engine"].begin() as connection:
        proposal_id = _insert_proposal(
            connection,
            setup,
            field_key="booking_reference_number",
            machine_value="BK-3001",
            confidence=0.98,
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_capture_proposals
                SET proposal_status='ACCEPTED',
                    accepted_value=CAST(:accepted_value AS jsonb),
                    accepted_by_actor_id='pc-correction-test',
                    accepted_by_role='PC',
                    accepted_at_utc=now()
                WHERE tenant_id=:tenant_id
                  AND capture_proposal_id=:proposal_id
                """
            ),
            {
                "tenant_id": setup["tenant_id"],
                "proposal_id": proposal_id,
                "accepted_value": json.dumps({"value": "BK-3001"}),
            },
        )
        count = connection.execute(
            text(
                """
                SELECT count(*)
                FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND rule_key='UC03_DI_CORRECTION_CONFIDENCE'
                """
            ),
            {"tenant_id": setup["tenant_id"], "journey_id": setup["journey_id"]},
        ).scalar_one()
        assert count == 0
