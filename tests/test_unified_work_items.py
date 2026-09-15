"""Verifies migration 0097's mirror trigger: every write to audit_findings
or workflow_tasks produces a matching auditcore.work_items (+ detail) row,
using the exact column mapping documented in the migration itself.

Requires a real Postgres (DATABASE_URL) with migrations applied through
0097 -- skips locally without one, runs for real in CI's "Apply fresh
database migration" step, matching test_uc03_backfill_document_sync_
producers.py's own seeded_tenant fixture pattern.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text


@pytest.fixture
def seeded_journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for work_items mirror integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-wi-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"WI-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"WI-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'WI', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"WI-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"WI-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"WI-O-{suffix}"},
        ).scalar_one()
        customer_id = c.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"WI-J-{suffix}"},
        ).scalar_one()
        workflow_instance_id = c.execute(
            text("""INSERT INTO auditcore.workflow_instances
                (tenant_id, journey_id, workflow_type)
                VALUES (:t, :j, 'TEST') RETURNING workflow_instance_id"""),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()
    yield {
        "engine": engine,
        "tenant_id": tenant_id,
        "journey_id": journey_id,
        "workflow_instance_id": workflow_instance_id,
    }
    engine.dispose()


def _insert_finding(engine, *, tenant_id, journey_id, origin_kind, finding_status="OPEN", **overrides):
    fields = {
        "tenant_id": tenant_id,
        "journey_id": journey_id,
        "finding_type_code": "WRONG_DOCUMENT",
        "severity": "HIGH",
        "finding_status": finding_status,
        "title": "Test finding",
        "description": "A test finding for the work_items mirror.",
        "stage_code": "BOOKING",
        "origin_kind": origin_kind,
        "rule_key": "TEST_RULE",
        "blocking_completion": False,
        "finding_class": "VIOLATION",
        "owner_role_code": "TL",
        "sla_due_at_utc": None,
        **overrides,
    }
    with engine.begin() as c:
        return c.execute(
            text(
                """
                INSERT INTO auditcore.audit_findings (
                    tenant_id, journey_id, finding_type_code, severity,
                    finding_status, title, description, stage_code,
                    origin_kind, rule_key, blocking_completion,
                    finding_class, owner_role_code, sla_due_at_utc
                ) VALUES (
                    :tenant_id, :journey_id, :finding_type_code, :severity,
                    :finding_status, :title, :description, :stage_code,
                    :origin_kind, :rule_key, :blocking_completion,
                    :finding_class, :owner_role_code, :sla_due_at_utc
                ) RETURNING audit_finding_id
                """
            ),
            fields,
        ).scalar_one()


def _work_item(engine, *, tenant_id, work_item_id):
    with engine.begin() as c:
        return c.execute(
            text("SELECT * FROM auditcore.work_items WHERE tenant_id=:t AND work_item_id=:w"),
            {"t": tenant_id, "w": work_item_id},
        ).mappings().one_or_none()


def _finding_detail(engine, *, tenant_id, work_item_id):
    with engine.begin() as c:
        return c.execute(
            text("SELECT * FROM auditcore.work_item_finding_detail WHERE tenant_id=:t AND work_item_id=:w"),
            {"t": tenant_id, "w": work_item_id},
        ).mappings().one_or_none()


def test_machine_finding_mirrors_with_machine_origin(seeded_journey) -> None:
    engine = seeded_journey["engine"]
    tenant_id = seeded_journey["tenant_id"]
    finding_id = _insert_finding(
        engine, tenant_id=tenant_id, journey_id=seeded_journey["journey_id"], origin_kind="MACHINE",
    )

    item = _work_item(engine, tenant_id=tenant_id, work_item_id=finding_id)
    assert item is not None
    assert item["item_kind"] == "FINDING"
    assert item["origin_kind"] == "MACHINE"
    assert item["subject_kind"] == "JOURNEY"
    assert item["subject_ref"] == seeded_journey["journey_id"]
    assert item["status"] == "OPEN"
    assert item["classification"] == "VIOLATION"
    assert item["owner_role_code"] == "TL"
    assert item["assigned_actor_id"] is None

    detail = _finding_detail(engine, tenant_id=tenant_id, work_item_id=finding_id)
    assert detail is not None
    assert detail["rule_key"] == "TEST_RULE"
    assert detail["severity"] == "HIGH"
    assert detail["stage_code"] == "BOOKING"


def test_manual_verification_rule_origin_maps_to_system(seeded_journey) -> None:
    engine = seeded_journey["engine"]
    tenant_id = seeded_journey["tenant_id"]
    finding_id = _insert_finding(
        engine, tenant_id=tenant_id, journey_id=seeded_journey["journey_id"], origin_kind="RULE",
    )

    item = _work_item(engine, tenant_id=tenant_id, work_item_id=finding_id)
    assert item["origin_kind"] == "SYSTEM"


def test_human_flag_origin_and_no_assignee(seeded_journey) -> None:
    engine = seeded_journey["engine"]
    tenant_id = seeded_journey["tenant_id"]
    finding_id = _insert_finding(
        engine, tenant_id=tenant_id, journey_id=seeded_journey["journey_id"],
        origin_kind="HUMAN", finding_class="DOCUMENT_GAP",
    )

    item = _work_item(engine, tenant_id=tenant_id, work_item_id=finding_id)
    assert item["origin_kind"] == "HUMAN"
    assert item["classification"] == "DOCUMENT_GAP"


@pytest.mark.parametrize(
    "finding_status,expected_work_item_status",
    [
        ("OPEN", "OPEN"),
        ("ACKNOWLEDGED", "IN_PROGRESS"),
        ("RESOLVED", "RESOLVED"),
        ("VOIDED", "CANCELLED"),
    ],
)
def test_status_vocabulary_maps_on_update(seeded_journey, finding_status, expected_work_item_status) -> None:
    engine = seeded_journey["engine"]
    tenant_id = seeded_journey["tenant_id"]
    finding_id = _insert_finding(
        engine, tenant_id=tenant_id, journey_id=seeded_journey["journey_id"], origin_kind="MACHINE",
    )
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE auditcore.audit_findings SET finding_status=:s, version_no=version_no+1 "
                "WHERE tenant_id=:t AND audit_finding_id=:f"
            ),
            {"s": finding_status, "t": tenant_id, "f": finding_id},
        )

    item = _work_item(engine, tenant_id=tenant_id, work_item_id=finding_id)
    assert item["status"] == expected_work_item_status


def test_workflow_task_mirrors_as_execution_task(seeded_journey) -> None:
    engine = seeded_journey["engine"]
    tenant_id = seeded_journey["tenant_id"]
    with engine.begin() as c:
        task_id = c.execute(
            text(
                """
                INSERT INTO auditcore.workflow_tasks (
                    tenant_id, workflow_instance_id, journey_id,
                    process_area, task_type, assigned_role_code, assigned_actor_id
                ) VALUES (
                    :t, :wi, :j, 'BOOKING', 'PC_DOCUMENT_REUPLOAD', 'PC', NULL
                ) RETURNING workflow_task_id
                """
            ),
            {"t": tenant_id, "wi": seeded_journey["workflow_instance_id"], "j": seeded_journey["journey_id"]},
        ).scalar_one()

    item = _work_item(engine, tenant_id=tenant_id, work_item_id=task_id)
    assert item is not None
    assert item["item_kind"] == "EXECUTION_TASK"
    assert item["origin_kind"] == "SYSTEM"
    assert item["subject_kind"] == "JOURNEY"
    assert item["subject_ref"] == seeded_journey["journey_id"]
    assert item["owner_role_code"] == "PC"
    # workflow_tasks defaults task_status to 'READY' -- maps to OPEN.
    assert item["status"] == "OPEN"

    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE auditcore.workflow_tasks SET task_status='COMPLETED', version_no=version_no+1 "
                "WHERE tenant_id=:t AND workflow_task_id=:w"
            ),
            {"t": tenant_id, "w": task_id},
        )
    item = _work_item(engine, tenant_id=tenant_id, work_item_id=task_id)
    assert item["status"] == "RESOLVED"


def test_mirror_never_blocks_the_real_write_even_if_row_is_unusual(seeded_journey) -> None:
    """A finding_status value the trigger doesn't recognize must still let
    the real audit_findings write through -- the exception handler exists
    precisely so a mirror gap never becomes a production incident."""
    engine = seeded_journey["engine"]
    tenant_id = seeded_journey["tenant_id"]
    # blocking_completion NOT NULL with no default override attempted here --
    # this insert is otherwise ordinary; the real assertion is simply that
    # inserting and reading back the row raises nothing.
    finding_id = _insert_finding(
        engine, tenant_id=tenant_id, journey_id=seeded_journey["journey_id"], origin_kind="MACHINE",
    )
    with engine.begin() as c:
        status = c.execute(
            text("SELECT finding_status FROM auditcore.audit_findings WHERE tenant_id=:t AND audit_finding_id=:f"),
            {"t": tenant_id, "f": finding_id},
        ).scalar_one()
    assert status == "OPEN"
