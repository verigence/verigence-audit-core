from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.workflow import claim_worker_task, get_workflow_task, start_worker_task
from audit_core.workflow_reliability import create_workflow_task_once, fail_worker_task


def test_effect_key_is_idempotent_and_retry_exhaustion_is_visible() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for workflow reliability test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-idempotency-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"ICAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Idempotency OEM') RETURNING oem_id"
            ),
            {"code": f"IOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Idempotency Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"IP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Idempotency Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"ID-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Idempotency Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"IO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Idempotency Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'IDEMPOTENCY-JOURNEY'
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

        effect_key = f"once-{suffix}"
        create_args = {
            "tenant_id": tenant_id,
            "effect_key": effect_key,
            "journey_id": journey_id,
            "workflow_type": "AUDIT_WORKER",
            "process_area": "AUDIT",
            "task_type": "RECONCILE",
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "correlation_id": f"idempotency-{suffix}",
        }
        first_task_id = create_workflow_task_once(connection, **create_args)
        replayed_task_id = create_workflow_task_once(connection, **create_args)
        assert replayed_task_id == first_task_id

        counts = connection.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM auditcore.workflow_tasks
                     WHERE tenant_id = :tenant_id AND effect_key = :effect_key) AS task_count,
                    (SELECT count(*) FROM auditcore.workflow_instances
                     WHERE tenant_id = :tenant_id AND journey_id = :journey_id) AS instance_count
                """
            ),
            {
                "tenant_id": tenant_id,
                "effect_key": effect_key,
                "journey_id": journey_id,
            },
        ).mappings().one()
        assert counts["task_count"] == 1
        assert counts["instance_count"] == 1

        connection.execute(
            text(
                """
                UPDATE auditcore.workflow_tasks
                SET max_attempts = 1
                WHERE tenant_id = :tenant_id AND workflow_task_id = :task_id
                """
            ),
            {"tenant_id": tenant_id, "task_id": first_task_id},
        )
        assert claim_worker_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=first_task_id,
            worker_id="worker-final",
            lease_seconds=60,
        ) == 1
        start_worker_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=first_task_id,
            worker_id="worker-final",
            lease_seconds=60,
        )
        result = fail_worker_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=first_task_id,
            worker_id="worker-final",
            retry_after_seconds=30,
            error_code="DEPENDENCY_UNAVAILABLE",
            error_summary="Dependency remained unavailable",
        )
        assert result == "DEAD_LETTER"

        dead_letter = get_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=first_task_id,
        )
        assert dead_letter["task_status"] == "DEAD_LETTER"
        assert dead_letter["attempt_count"] == 1
        assert dead_letter["lease_owner"] is None
        assert dead_letter["next_attempt_at_utc"] is None
        assert dead_letter["last_error_code"] == "DEPENDENCY_UNAVAILABLE"

        attempt = connection.execute(
            text(
                """
                SELECT attempt_result, error_code, next_retry_at_utc
                FROM auditcore.workflow_task_attempts
                WHERE tenant_id = :tenant_id AND workflow_task_id = :task_id
                  AND attempt_no = 1
                """
            ),
            {"tenant_id": tenant_id, "task_id": first_task_id},
        ).mappings().one()
        assert attempt["attempt_result"] == "RETRYABLE_FAILURE"
        assert attempt["error_code"] == "DEPENDENCY_UNAVAILABLE"
        assert attempt["next_retry_at_utc"] is None

        dead_letter_event = connection.execute(
            text(
                """
                SELECT from_status, to_status
                FROM auditcore.workflow_task_events
                WHERE tenant_id = :tenant_id AND workflow_task_id = :task_id
                  AND event_type = 'RETRIES_EXHAUSTED'
                """
            ),
            {"tenant_id": tenant_id, "task_id": first_task_id},
        ).mappings().one()
        assert dead_letter_event["from_status"] == "IN_PROGRESS"
        assert dead_letter_event["to_status"] == "DEAD_LETTER"

    engine.dispose()
