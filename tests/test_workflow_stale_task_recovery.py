from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.workflow import (
    claim_worker_task,
    create_workflow_task,
    get_workflow_task,
)
from audit_core.workflow_stale_task_recovery import (
    recover_stale_worker_tasks_for_all_tenants,
)


def _seed_project_with_stale_task(engine, *, tenant_id: str, suffix: str) -> tuple:
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"SWCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Sweep OEM') RETURNING oem_id"
            ),
            {"code": f"SWOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Sweep Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"SWP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Sweep Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"SWD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Sweep Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"SWO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Sweep Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'SWEEP-JOURNEY'
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
        task_id = create_workflow_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            workflow_type="AUDIT_WORKER",
            process_area="AUDIT",
            task_type="RECONCILE",
            dealer_id=dealer_id,
            outlet_id=outlet_id,
            effect_key=f"sweep-effect-{suffix}",
            correlation_id=f"sweep-{suffix}",
        )
        claim_worker_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            worker_id="worker-1",
            lease_seconds=60,
        )
        # Backdate the lease so this task looks abandoned -- a worker that
        # claimed it and then crashed/restarted without finishing.
        connection.execute(
            text(
                """
                UPDATE auditcore.workflow_tasks
                SET lease_acquired_at_utc = now() - interval '10 minutes',
                    lease_heartbeat_at_utc = now() - interval '10 minutes',
                    lease_expires_at_utc = now() - interval '9 minutes'
                WHERE tenant_id = :tenant_id AND workflow_task_id = :task_id
                """
            ),
            {"tenant_id": tenant_id, "task_id": task_id},
        )
    return task_id


def test_sweep_recovers_a_stale_task_across_tenants_it_discovers_itself() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the stale-task sweep test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-sweep-{suffix}"
    task_id = _seed_project_with_stale_task(engine, tenant_id=tenant_id, suffix=suffix)

    # No tenant context is set anywhere in this test before calling the sweep --
    # it must discover the tenant on its own via the super-admin project
    # directory, exactly like a real periodic tick with no request-scoped
    # tenant in hand.
    recovered_total = recover_stale_worker_tasks_for_all_tenants(engine)
    assert recovered_total >= 1

    with engine.begin() as connection:
        task = get_workflow_task(connection, tenant_id=tenant_id, workflow_task_id=task_id)
    assert task["task_status"] == "READY"
    assert task["lease_owner"] is None
    assert task["last_error_code"] == "LEASE_LOST"

    engine.dispose()


def test_sweep_skips_a_failing_tenant_without_aborting_the_rest() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the stale-task sweep test")

    engine = create_engine(database_url)
    good_suffix = uuid4().hex
    bad_suffix = uuid4().hex
    good_tenant_id = f"tenant-sweep-good-{good_suffix}"
    bad_tenant_id = f"tenant-sweep-bad-{bad_suffix}"
    good_task_id = _seed_project_with_stale_task(
        engine, tenant_id=good_tenant_id, suffix=good_suffix
    )
    _seed_project_with_stale_task(engine, tenant_id=bad_tenant_id, suffix=bad_suffix)

    # Prove the per-tenant try/except actually isolates one tenant's failure
    # from the rest of the sweep, by making recovery blow up for exactly the
    # "bad" tenant and confirming the "good" one still gets recovered in the
    # same sweep call.
    import audit_core.workflow_stale_task_recovery as sweep_module

    original = sweep_module.recover_stale_worker_tasks

    def _boom(connection, *, tenant_id, limit=100):
        if tenant_id == bad_tenant_id:
            raise RuntimeError("simulated per-tenant failure")
        return original(connection, tenant_id=tenant_id, limit=limit)

    sweep_module.recover_stale_worker_tasks = _boom
    try:
        recovered_total = sweep_module.recover_stale_worker_tasks_for_all_tenants(engine)
    finally:
        sweep_module.recover_stale_worker_tasks = original

    assert recovered_total >= 1
    with engine.begin() as connection:
        task = get_workflow_task(
            connection, tenant_id=good_tenant_id, workflow_task_id=good_task_id
        )
    assert task["task_status"] == "READY"

    engine.dispose()
