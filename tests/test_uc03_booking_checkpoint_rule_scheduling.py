from __future__ import annotations

import inspect
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_confidence_review_policy as confidence_policy
from audit_core.uc03_booking_rule_trigger import schedule_booking_checkpoint_rules


def _seed_booking_journey(engine, *, tenant_id: str, suffix: str, version_no: int = 1):
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"CKCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Checkpoint OEM') RETURNING oem_id"
            ),
            {"code": f"CKOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Checkpoint Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"CKP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Checkpoint Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"CKD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Checkpoint Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"CKO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Checkpoint Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'CHECKPOINT-JOURNEY'
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
                INSERT INTO auditcore.journey_stage_states (
                    tenant_id, journey_id, stage_code, business_status, audit_state,
                    audit_status, first_started_at_utc, latest_activity_at_utc, version_no
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING', 'BOOKING_IN_PROGRESS', 'IN_PROGRESS',
                    'NOT_EVALUATED', now(), now(), :version_no
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version_no": version_no},
        )
    return journey_id


def test_schedule_booking_checkpoint_rules_dedupes_by_aggregate_version() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the checkpoint-rule scheduling test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-checkpoint-{suffix}"
    journey_id = _seed_booking_journey(engine, tenant_id=tenant_id, suffix=suffix, version_no=3)

    # Two calls for the SAME aggregate version (e.g. the async document-sync
    # path firing, then the confirm safety net firing moments later with
    # nothing having changed in between) must collapse to exactly one
    # workflow_task, not one per call.
    schedule_booking_checkpoint_rules(
        engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        correlation_id="test-1",
        trigger="ASYNC_DOCUMENT_SYNC",
    )
    schedule_booking_checkpoint_rules(
        engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        correlation_id="test-2",
        trigger="PC_BOOKING_ATTRIBUTE_REVIEW_CONFIRMED",
    )

    with engine.begin() as connection:
        tasks = connection.execute(
            text(
                """
                SELECT task_status, task_type
                FROM auditcore.workflow_tasks
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND task_type='BOOKING_RULE_EVALUATION'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().all()
    assert len(tasks) == 1
    assert tasks[0]["task_status"] == "COMPLETED"

    engine.dispose()


def test_schedule_booking_checkpoint_rules_is_a_clean_skip_with_no_stage_state() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the checkpoint-rule scheduling test")

    engine = create_engine(database_url)
    # No journey_stage_states row exists for this journey_id -- the function
    # must return quietly, not raise, exactly like a document syncing for a
    # journey mid-provisioning.
    schedule_booking_checkpoint_rules(
        engine,
        tenant_id=f"tenant-checkpoint-missing-{uuid4().hex}",
        journey_id=uuid4(),
        correlation_id="test",
        trigger="ASYNC_DOCUMENT_SYNC",
    )
    engine.dispose()


def test_confirm_handler_schedules_checkpoint_rules_as_a_safety_net() -> None:
    # Regression test for the bug this fix closes: confirm_booking_review_v2_
    # confidence_policy is the actually-live handler for POST .../booking/
    # review/confirm (confirmed by install order in uc03_document_capture_v2_
    # rules.py), but until this fix it never called schedule_booking_
    # checkpoint_rules (nor its predecessor) anywhere in its body -- the
    # Booking checkpoint rules and the external rule-engine phase silently
    # never ran on a live confirm. Source-inspected rather than exercised
    # end-to-end because the full confirm flow needs a large fixture
    # (attributes, documents, decisions) that adds nothing to this specific
    # assertion.
    source = inspect.getsource(confidence_policy.confirm_booking_review_v2_confidence_policy)
    assert "background_tasks: BackgroundTasks" in source
    assert "background_tasks.add_task(" in source
    assert "schedule_booking_checkpoint_rules" in source


def test_async_document_sync_schedules_checkpoint_rules_for_booking() -> None:
    # The primary trigger this fix adds: every Booking document confirming
    # through the DI webhook's background sync evaluates checkpoint rules on
    # its own, so the PC never has to remember to click Confirm for rule
    # findings to surface.
    source = inspect.getsource(confidence_policy._run_sync_booking_document_task)
    assert 'stage_code == "BOOKING"' in source
    assert "schedule_booking_checkpoint_rules(" in source
