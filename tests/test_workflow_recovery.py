from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.workflow import (
    claim_worker_task,
    create_workflow_task,
    get_workflow_task,
    heartbeat_worker_task,
    recover_stale_worker_tasks,
    schedule_worker_retry,
    start_worker_task,
)


def test_worker_retry_and_stale_lease_recovery_reuse_the_same_task() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for workflow recovery test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-recovery-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"RCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Recovery OEM') RETURNING oem_id"
            ),
            {"code": f"ROEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Recovery Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"RP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Recovery Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"RD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Recovery Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"RO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Recovery Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'RECOVERY-JOURNEY'
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
        effect_key = f"recovery-effect-{suffix}"
        task_id = create_workflow_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            workflow_type="AUDIT_WORKER",
            process_area="AUDIT",
            task_type="RECONCILE",
            dealer_id=dealer_id,
            outlet_id=outlet_id,
            effect_key=effect_key,
            correlation_id=f"recovery-{suffix}",
        )

        attempt_no = claim_worker_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            worker_id="worker-1",
            lease_seconds=60,
        )
        assert attempt_no == 1
        start_worker_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            worker_id="worker-1",
            lease_seconds=60,
        )
        heartbeat_worker_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            worker_id="worker-1",
            lease_seconds=60,
        )
        schedule_worker_retry(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            worker_id="worker-1",
            retry_after_seconds=0,
            error_code="TEMPORARY_DEPENDENCY",
            error_summary="Dependency temporarily unavailable",
        )
        retry_wait = get_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
        )
        assert retry_wait["task_status"] == "RETRY_WAIT"
        assert retry_wait["attempt_count"] == 1
        assert retry_wait["lease_owner"] is None

        second_attempt = claim_worker_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            worker_id="worker-2",
            lease_seconds=60,
        )
        assert second_attempt == 2
        start_worker_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            worker_id="worker-2",
            lease_seconds=60,
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.workflow_tasks
                SET lease_acquired_at_utc = now() - interval '2 minutes',
                    lease_heartbeat_at_utc = now() - interval '2 minutes',
                    lease_expires_at_utc = now() - interval '1 minute'
                WHERE tenant_id = :tenant_id AND workflow_task_id = :task_id
                """
            ),
            {"tenant_id": tenant_id, "task_id": task_id},
        )

        recovered_ids = recover_stale_worker_tasks(
            connection,
            tenant_id=tenant_id,
        )
        assert recovered_ids == [task_id]
        recovered = get_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
        )
        assert recovered["task_status"] == "READY"
        assert recovered["attempt_count"] == 2
        assert recovered["lease_owner"] is None
        assert recovered["last_error_code"] == "LEASE_LOST"

        third_attempt = claim_worker_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            worker_id="worker-3",
            lease_seconds=60,
        )
        assert third_attempt == 3

        attempt_results = connection.execute(
            text(
                """
                SELECT attempt_no, attempt_result
                FROM auditcore.workflow_task_attempts
                WHERE tenant_id = :tenant_id AND workflow_task_id = :task_id
                ORDER BY attempt_no
                """
            ),
            {"tenant_id": tenant_id, "task_id": task_id},
        ).all()
        assert attempt_results[0] == (1, "RETRYABLE_FAILURE")
        assert attempt_results[1] == (2, "LEASE_LOST")
        assert attempt_results[2] == (3, None)

        task_count = connection.execute(
            text(
                """
                SELECT count(*)
                FROM auditcore.workflow_tasks
                WHERE tenant_id = :tenant_id AND effect_key = :effect_key
                """
            ),
            {"tenant_id": tenant_id, "effect_key": effect_key},
        ).scalar_one()
        assert task_count == 1

    engine.dispose()
