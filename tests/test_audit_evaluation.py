from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.audit_evaluation import evaluate_control, get_evaluation


def test_evaluation_keeps_exact_control_version_and_input_snapshots() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for audit evaluation integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-evaluation-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"ECAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Evaluation OEM') RETURNING oem_id"
            ),
            {"code": f"EOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Evaluation Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"EP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Evaluation Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"ED-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Evaluation Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"EO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Evaluation Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'EVAL-JOURNEY'
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
        control_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_controls (
                    tenant_id, control_key, control_name, process_area
                ) VALUES (
                    :tenant_id, :control_key, 'Payment snapshot match', 'PAYMENT'
                ) RETURNING audit_control_id
                """
            ),
            {"tenant_id": tenant_id, "control_key": f"CTRL-{suffix}"},
        ).scalar_one()

        v1 = connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_control_versions (
                    tenant_id, audit_control_id, version_no, effective_from,
                    evaluator_key, rule_config
                ) VALUES (
                    :tenant_id, :control_id, 1, CURRENT_DATE,
                    'EXACT_SNAPSHOT_MATCH', CAST(:rule_config AS jsonb)
                ) RETURNING audit_control_version_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "control_id": control_id,
                "rule_config": json.dumps(
                    {
                        "expectedSnapshot": {
                            "paymentStatus": "RECEIVED",
                            "sourceEvidenceId": "evidence-1",
                        }
                    }
                ),
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                UPDATE auditcore.audit_control_versions
                SET lifecycle_status = 'PUBLISHED', published_at_utc = now()
                WHERE tenant_id = :tenant_id
                  AND audit_control_version_id = :version_id
                """
            ),
            {"tenant_id": tenant_id, "version_id": v1},
        )

        observed = {
            "paymentStatus": "RECEIVED",
            "sourceEvidenceId": "evidence-1",
        }
        first_id = evaluate_control(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            audit_control_version_id=v1,
            observed_snapshot=observed,
            correlation_id="eval-1",
        )
        first = get_evaluation(
            connection,
            tenant_id=tenant_id,
            evaluation_id=first_id,
        )
        assert first["evaluation_result"] == "PASS"
        assert first["audit_control_version_id"] == v1
        assert first["observed_snapshot"]["sourceEvidenceId"] == "evidence-1"

        connection.execute(
            text(
                """
                UPDATE auditcore.audit_control_versions
                SET lifecycle_status = 'RETIRED'
                WHERE tenant_id = :tenant_id
                  AND audit_control_version_id = :version_id
                """
            ),
            {"tenant_id": tenant_id, "version_id": v1},
        )
        v2 = connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_control_versions (
                    tenant_id, audit_control_id, version_no, effective_from,
                    evaluator_key, rule_config
                ) VALUES (
                    :tenant_id, :control_id, 2, CURRENT_DATE,
                    'EXACT_SNAPSHOT_MATCH', CAST(:rule_config AS jsonb)
                ) RETURNING audit_control_version_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "control_id": control_id,
                "rule_config": json.dumps(
                    {
                        "expectedSnapshot": {
                            "paymentStatus": "CLEARED",
                            "sourceEvidenceId": "evidence-1",
                        }
                    }
                ),
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                UPDATE auditcore.audit_control_versions
                SET lifecycle_status = 'PUBLISHED', published_at_utc = now()
                WHERE tenant_id = :tenant_id
                  AND audit_control_version_id = :version_id
                """
            ),
            {"tenant_id": tenant_id, "version_id": v2},
        )

        second_id = evaluate_control(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            audit_control_version_id=v2,
            observed_snapshot=observed,
            correlation_id="eval-2",
        )
        second = get_evaluation(
            connection,
            tenant_id=tenant_id,
            evaluation_id=second_id,
        )
        assert second["evaluation_result"] == "FAIL"
        assert second["audit_control_version_id"] == v2
        assert second["expected_snapshot"] != first["expected_snapshot"]
        assert second["observed_snapshot"] == first["observed_snapshot"]

    engine.dispose()
