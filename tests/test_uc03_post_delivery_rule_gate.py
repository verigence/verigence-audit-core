from __future__ import annotations

import inspect
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.uc03_post_delivery_rule_gate import (
    _effect_key,
    complete_post_delivery_rule_gate,
    ensure_post_delivery_rule_task,
    post_delivery_rule_gate_status,
)
from audit_core.workflow import claim_worker_task, start_worker_task


def test_post_delivery_effect_key_is_version_scoped() -> None:
    journey_id = uuid4()

    assert _effect_key(journey_id, 1) == (
        f"uc03.post-delivery-rule-run:{journey_id}:1"
    )
    assert _effect_key(journey_id, 2) != _effect_key(journey_id, 1)
    with pytest.raises(ValueError):
        _effect_key(journey_id, 0)


def test_rule_gate_reuses_existing_workflow_reliability_boundary() -> None:
    ensure_source = inspect.getsource(ensure_post_delivery_rule_task)
    complete_source = inspect.getsource(complete_post_delivery_rule_gate)
    module_source = inspect.getsource(
        inspect.getmodule(complete_post_delivery_rule_gate)
    )

    assert "create_workflow_task_once(" in ensure_source
    assert 'task_type=_TASK_TYPE' in ensure_source
    assert 'process_area=_PROCESS_AREA' in ensure_source
    assert "evaluate_control(" not in module_source
    assert "get_di_client" not in module_source
    assert "e.process_area='POST_DELIVERY'" in module_source
    assert "f.stage_code" not in module_source
    assert "attempt_result='SUCCEEDED'" in module_source
    assert "lease_expires_at_utc > now()" in module_source
    assert "audit_state='COMPLETE'" in complete_source


def test_post_delivery_rule_task_and_report_readiness_db() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for post-Delivery rule gate DB test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-post-delivery-{suffix}"
    worker_id = f"post-delivery-worker-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories "
                "(category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"PDCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Post Delivery OEM') RETURNING oem_id"
            ),
            {"code": f"PDOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Post Delivery Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"PDP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers "
                "(tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Post Delivery Dealer') "
                "RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"PDD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Post Delivery Outlet'
                ) RETURNING outlet_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "code": f"PDO-{suffix}",
            },
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id,
                    customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id,
                    'RETAIL', 'Post Delivery Customer'
                ) RETURNING customer_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
            },
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id,
                    customer_id, journey_reference
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id,
                    :customer_id, :journey_reference
                ) RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
                "customer_id": customer_id,
                "journey_reference": f"POST-DELIVERY-{suffix}",
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_stage_states (
                    tenant_id, journey_id, stage_code,
                    audit_state, audit_status,
                    first_started_at_utc, latest_activity_at_utc,
                    version_no
                ) VALUES (
                    :tenant_id, :journey_id, 'POST_DELIVERY',
                    'IN_PROGRESS', 'NOT_EVALUATED', now(), now(), 1
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        )

        first_task_id = ensure_post_delivery_rule_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            finalization_version=1,
            correlation_id=f"post-delivery-{suffix}",
        )
        replayed_task_id = ensure_post_delivery_rule_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            finalization_version=1,
            correlation_id=f"post-delivery-{suffix}",
        )
        assert replayed_task_id == first_task_id

        pending = post_delivery_rule_gate_status(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
        assert pending["ruleTaskId"] == first_task_id
        assert pending["ruleTaskStatus"] == "READY"
        assert pending["postDeliveryAuditState"] == "IN_PROGRESS"
        assert pending["reportReady"] is False

        assert claim_worker_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=first_task_id,
            worker_id=worker_id,
            lease_seconds=60,
        ) == 1
        start_worker_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=first_task_id,
            worker_id=worker_id,
            lease_seconds=60,
        )
        complete_post_delivery_rule_gate(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            workflow_task_id=first_task_id,
            worker_id=worker_id,
        )

        ready = post_delivery_rule_gate_status(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
        assert ready["ruleTaskStatus"] == "COMPLETED"
        assert ready["postDeliveryAuditState"] == "COMPLETE"
        assert ready["postDeliveryAuditStatus"] == "NO_FLAGS"
        assert ready["reportReady"] is True

        attempt = connection.execute(
            text(
                """
                SELECT attempt_result, ended_at_utc
                FROM auditcore.workflow_task_attempts
                WHERE tenant_id=:tenant_id
                  AND workflow_task_id=:task_id
                  AND attempt_no=1
                """
            ),
            {"tenant_id": tenant_id, "task_id": first_task_id},
        ).mappings().one()
        assert attempt["attempt_result"] == "SUCCEEDED"
        assert attempt["ended_at_utc"] is not None

        counts = connection.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM auditcore.workflow_tasks
                     WHERE tenant_id=:tenant_id
                       AND effect_key=:effect_key) AS task_count,
                    (SELECT count(*) FROM auditcore.workflow_instances
                     WHERE tenant_id=:tenant_id
                       AND journey_id=:journey_id
                       AND workflow_type='UC03_POST_DELIVERY_AUDIT') AS instance_count
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "effect_key": _effect_key(journey_id, 1),
            },
        ).mappings().one()
        assert counts["task_count"] == 1
        assert counts["instance_count"] == 1

    engine.dispose()
