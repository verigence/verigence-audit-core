"""The worker asks itself for no work (real Postgres, 2026-10-31).

System Health showed 318 items "waiting for more than 30 minutes" and claim
counts up to 251 on one Journey's recompute. A PC's one correction ran two
recomputes and every control unit twice: the recompute closed the field's
review task, the task trigger bumped the fact version silently, the recompute
could not store its fingerprint, the fact sweep asked again -- and each
recompute rewrote the deal's rows (updated_at_utc, version_no), which changed
every unit's fingerprint. These tests hold the queue to: a run over facts
that did not change asks for nothing more.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
    set_minimum_booking_amount,
)
from sqlalchemy import text

from audit_core import uc03_p2_controls as controls
from audit_core import uc03_p2_journey_delete as journey_delete
from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_runtime import (
    enqueue_work,
    fact_fingerprint,
    mark_derived_writes,
    note_facts_changed,
)
from audit_core.uc03_p2_tasks import create_p2_task


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="churn")
    set_minimum_booking_amount(created, "21000")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _one(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params}).first()


def _runtime(journey) -> tuple[int, str]:
    row = _one(journey, "SELECT fact_version, fact_fingerprint FROM auditcore.p2_journey_runtime "
                        "WHERE tenant_id=:t AND journey_id=:j")
    return int(row[0]), row[1]


def _current_fingerprint(journey) -> str:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return fact_fingerprint(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _unit_runs(journey) -> dict[str, datetime]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        rows = connection.execute(
            text("SELECT unit_key, last_evaluated_at_utc FROM auditcore.p2_control_units "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).all()
    return {r[0]: r[1] for r in rows}


def _recompute_claims(journey) -> int:
    return int(_one(journey, "SELECT claim_count FROM auditcore.p2_work_queue "
                             "WHERE tenant_id=:t AND journey_id=:j AND work_type='STAGE_RECOMPUTE'")[0])


def _drain(journey, monkeypatch) -> None:
    """Run every item the Journey has, as the worker would, until none is left
    (delays skipped). The Rule Engine is not called."""
    monkeypatch.setattr(controls, "_evaluate_external", lambda *a, **k: [])
    for _ in range(12):
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            connection.execute(text("UPDATE auditcore.p2_work_queue SET next_attempt_at_utc=NULL "
                                    "WHERE tenant_id=:t AND work_status='PENDING'"), {"t": journey.tenant_id})
        items = worker._claim_for_tenant(journey.engine, journey.tenant_id, 20)
        if not items:
            return
        for item in items:
            worker.process_work(journey.engine, item)
            worker._complete(journey.engine, item)
    raise AssertionError("the queue never emptied: the Journey keeps asking itself for work")


def _a_booking(journey) -> None:
    # A low-confidence value: the recompute raises the PC's review task for it.
    add_ready_document(journey, "booking_form", model="SCORPIO N", variant="Z8L", color="RED",
                       ex_showroom_price="1500000", customer_name="P2 Customer", booking_date="2026-10-01")
    add_ready_document(journey, "pan_card", confidence=40.0, pan_number="ABCDE1234F", name="P2 Customer")
    add_ready_document(journey, "aadhaar", aadhaar_number="123412341234", name="P2 Customer")
    add_receipt_payment(journey, amount="25000", receipt_number="R1", receipt_date="2026-10-01")


def test_a_run_over_unchanged_facts_asks_for_nothing_more(journey, monkeypatch):
    _a_booking(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        note_facts_changed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                           reason="DOCUMENT_READY")
    _drain(journey, monkeypatch)
    # The recompute raised the review task without asking for itself again,
    # so it stored its fingerprint: the fact sweep has nothing to find.
    assert _one(journey, "SELECT COUNT(*) FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                         "AND task_type='MANUAL_VERIFICATION_REVIEW'")[0] >= 1
    version, stored = _runtime(journey)
    assert version == 1, "the recompute bumped the fact version itself"
    assert stored == _current_fingerprint(journey)
    assert worker._fact_sweep(journey.engine, journey.tenant_id) == 0
    assert _recompute_claims(journey) == 1
    units = _unit_runs(journey)
    assert units

    # Asked again with nothing new: one recompute, no unit runs, no new version.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        asked = note_facts_changed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                   reason="MANUAL_RECHECK")
    _drain(journey, monkeypatch)
    assert _runtime(journey) == (asked, stored)
    assert _recompute_claims(journey) == 2
    assert _unit_runs(journey) == units, "a recompute that changed nothing re-ran the control units"
    assert worker._fact_sweep(journey.engine, journey.tenant_id) == 0


def test_a_review_task_the_recompute_closes_asks_for_no_recompute_but_a_persons_change_does(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        note_facts_changed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, reason="T")
        task_id = create_p2_task(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            task_type="MANUAL_VERIFICATION_REVIEW", category="DOCUMENT_VERIFICATION", origin_kind="SYSTEM",
            source_type="DOCUMENT_FIELD", source_code="FIELD_REVIEW", dedupe_key=f"test:{uuid4()}",
            title="Review", description="Review it", reference={}, severity="HIGH", priority="HIGH",
            assigned_role_code="PC", assigned_actor_id=None, raised_by_actor_id=None, raised_by_role_code=None,
            allowed_actions=["REVIEW_DOCUMENT"], completion_protocol="MACHINE_VERIFIED",
        )
    raised = _runtime(journey)[0]
    assert raised == 2  # the trigger: a person's (or any other path's) task asks for a recompute
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        mark_derived_writes(connection)
        connection.execute(text("UPDATE auditcore.p2_tasks SET task_status='VERIFIED_COMPLETE' "
                                "WHERE tenant_id=:t AND task_id=:id"), {"t": journey.tenant_id, "id": task_id})
    assert _runtime(journey)[0] == raised
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_tasks SET task_status='RETURNED' "
                                "WHERE tenant_id=:t AND task_id=:id"), {"t": journey.tenant_id, "id": task_id})
    assert _runtime(journey)[0] == raised + 1


def test_rewriting_the_deal_with_the_same_values_keeps_the_fingerprint(journey):
    add_receipt_payment(journey, amount="25000", receipt_number="R1", receipt_date="2026-10-01")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.journey_products (tenant_id, journey_id, model_name_snapshot, "
                 "colour_name_snapshot, selection_source, match_evidence) "
                 "VALUES (:t, :j, 'SCORPIO N', 'RED', 'EVIDENCE', '{\"decidedAtUtc\": \"2026-10-30\"}')"),
            {"t": journey.tenant_id, "j": journey.journey_id})
        connection.execute(
            text("INSERT INTO auditcore.insurance_records (tenant_id, journey_id, insurer_name, source_kind) "
                 "VALUES (:t, :j, 'ICICI', 'EVIDENCE')"), {"t": journey.tenant_id, "j": journey.journey_id})
    before = _current_fingerprint(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        params = {"t": journey.tenant_id, "j": journey.journey_id}
        connection.execute(text("UPDATE auditcore.payments SET amount=amount, version_no=version_no+1, "
                                "updated_at_utc=now() + interval '1 second' WHERE tenant_id=:t AND journey_id=:j"),
                           params)
        connection.execute(text("UPDATE auditcore.journey_products SET colour_name_snapshot='RED', "
                                "match_evidence='{\"decidedAtUtc\": \"2026-10-31\"}', "
                                "updated_at_utc=now() + interval '1 second' WHERE tenant_id=:t AND journey_id=:j"),
                           params)
        connection.execute(text("UPDATE auditcore.insurance_records SET version_no=version_no+1, "
                                "updated_at_utc=now() + interval '1 second' WHERE tenant_id=:t AND journey_id=:j"),
                           params)
    assert _current_fingerprint(journey) == before, "a rewrite with the same values read as a fact change"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.journey_products SET colour_name_snapshot='WHITE' "
                                "WHERE tenant_id=:t AND journey_id=:j"),
                           {"t": journey.tenant_id, "j": journey.journey_id})
    assert _current_fingerprint(journey) != before


def test_a_finished_recompute_asked_for_again_joins_the_back_of_the_line(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        note_facts_changed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, reason="T")
        connection.execute(
            text("UPDATE auditcore.p2_work_queue SET work_status='COMPLETED', queued_at_utc=now() - interval '12 days' "
                 "WHERE tenant_id=:t AND journey_id=:j AND work_type='STAGE_RECOMPUTE'"),
            {"t": journey.tenant_id, "j": journey.journey_id})
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        note_facts_changed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, reason="T")
    waiting = _one(journey, "SELECT now() - queued_at_utc FROM auditcore.p2_work_queue "
                            "WHERE tenant_id=:t AND journey_id=:j AND work_type='STAGE_RECOMPUTE'")[0]
    assert waiting < timedelta(minutes=1)
    # one already waiting keeps its place
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_work_queue SET queued_at_utc=now() - interval '5 minutes' "
                                "WHERE tenant_id=:t AND journey_id=:j AND work_type='STAGE_RECOMPUTE'"),
                           {"t": journey.tenant_id, "j": journey.journey_id})
        note_facts_changed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, reason="T")
    waiting = _one(journey, "SELECT now() - queued_at_utc FROM auditcore.p2_work_queue "
                            "WHERE tenant_id=:t AND journey_id=:j AND work_type='STAGE_RECOMPUTE'")[0]
    assert waiting > timedelta(minutes=4)


def test_a_journeys_checks_wait_for_its_recompute(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type="CONTROL_EVALUATE", work_key=f"unit:{journey.journey_id}:P2:DERIVED",
                     payload={"unit": "P2:DERIVED"}, correlation_id=None)
        connection.execute(text("UPDATE auditcore.p2_work_queue SET queued_at_utc=now() - interval '1 minute' "
                                "WHERE tenant_id=:t AND journey_id=:j"),
                           {"t": journey.tenant_id, "j": journey.journey_id})
        note_facts_changed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, reason="T")
    [first] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 5)
    assert first.work_type == "STAGE_RECOMPUTE"


def _delete_started(journey) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_journey_deletions (tenant_id, journey_id, reason, deleted_by_actor_id, "
                 "deleted_by_role) VALUES (:t, :j, 'Duplicate journey removed', 'SYSTEM', 'SYSTEM')"),
            {"t": journey.tenant_id, "j": journey.journey_id})


def test_a_journey_being_deleted_gets_no_new_work_but_its_delete_item_runs(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        note_facts_changed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, reason="T")
    _delete_started(journey)
    assert worker._claim_for_tenant(journey.engine, journey.tenant_id, 5) == []
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type=worker.SAME_FILE_DELETE_WORK_TYPE, work_key=str(journey.journey_id),
                     payload={}, correlation_id=None)
    [own] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 5)
    assert own.work_type == worker.SAME_FILE_DELETE_WORK_TYPE


def test_the_delete_waits_for_the_journeys_running_work(journey, monkeypatch):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        note_facts_changed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, reason="T")
    [running] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 5)
    monkeypatch.setattr(journey_delete, "_WORK_WAIT_ATTEMPTS", 2)
    monkeypatch.setattr(journey_delete, "_WORK_WAIT_SECONDS", 0)
    with (pytest.raises(journey_delete.JourneyWorkRunning),
          journey_delete._journey_work_held(journey.engine, tenant_id=journey.tenant_id,
                                            journey_id=journey.journey_id)):
        raise AssertionError("deleted under a running check")
    worker._complete(journey.engine, running)
    with journey_delete._journey_work_held(journey.engine, tenant_id=journey.tenant_id,
                                           journey_id=journey.journey_id) as connection:
        # its rows are held: no claim can start one until the delete commits
        assert worker._claim_for_tenant(journey.engine, journey.tenant_id, 5) == []
        assert connection is not None


def test_a_resolver_task_the_pc_marked_done_is_verified_not_dead_lettered(journey, monkeypatch):
    """Live 2026-10-30: a PC's "Review document" on a cost sheet / NDC task
    sent it to verification, which had no adapter for RESOLVER tasks: five
    RuntimeErrors and a dead letter."""
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        note_facts_changed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, reason="T")
        task_id = create_p2_task(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            task_type="COST_SHEET_NDC_DIFFERENCE", category="CROSS_DOCUMENT_REVIEW", origin_kind="SYSTEM",
            source_type="RESOLVER", source_code="COST_SHEET_NDC_DIFFERENCE", dedupe_key=f"test:{uuid4()}",
            title="Cost sheet and NDC differ", description="Check", reference={}, severity="CRITICAL",
            priority="HIGH", assigned_role_code="PC", assigned_actor_id=None, raised_by_actor_id=None,
            raised_by_role_code=None, allowed_actions=["REVIEW_DOCUMENT", "CLOSE_TASK", "ADD_COMMENT"],
            completion_protocol="MACHINE_VERIFIED",
        )
        connection.execute(text("UPDATE auditcore.p2_tasks SET task_status='VERIFYING' "
                                "WHERE tenant_id=:t AND task_id=:id"), {"t": journey.tenant_id, "id": task_id})
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type="TASK_VERIFY", work_key=str(task_id), payload={}, correlation_id=None)
        connection.execute(text("UPDATE auditcore.p2_work_queue SET work_status='COMPLETED' WHERE tenant_id=:t "
                                "AND journey_id=:j AND work_type='STAGE_RECOMPUTE'"),
                           {"t": journey.tenant_id, "j": journey.journey_id})
    [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 5)
    assert item.work_type == "TASK_VERIFY"
    worker.process_work(journey.engine, item)
    forced = _one(journey, "SELECT COUNT(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND journey_id=:j "
                           "AND work_type='CONTROL_EVALUATE' AND payload->>'force' = 'true'")[0]
    assert forced >= 3


def test_the_health_page_dates_a_waiting_item_from_when_it_became_due(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        params = {"t": journey.tenant_id, "j": journey.journey_id}
        for key, created, queued, due in (
            ("waiting", "40 minutes", "40 minutes", None),          # really waiting
            ("reused", "12 days", "2 minutes", None),               # asked again just now
            ("retry", "12 days", "50 minutes", "-5 minutes"),       # its retry delay ends in 5 minutes
        ):
            connection.execute(
                text("INSERT INTO auditcore.p2_work_queue (tenant_id, journey_id, work_type, work_key, "
                     "created_at_utc, queued_at_utc, next_attempt_at_utc, work_status) VALUES (:t, :j, "
                     "'JOURNEY_RECONCILE', :k, now() - CAST(:c AS interval), now() - CAST(:q AS interval), "
                     "CASE WHEN CAST(:d AS interval) IS NULL THEN NULL ELSE now() - CAST(:d AS interval) END, "
                     "CASE WHEN CAST(:d AS interval) IS NULL THEN 'PENDING' ELSE 'RETRY_WAIT' END)"),
                {**params, "k": f"{key}-{uuid4().hex}", "c": created, "q": queued, "d": due})
        raw = connection.execute(text("SELECT platform_ops.pipeline_health(now() - interval '24 hours')")).scalar_one()
    mine = [w for w in raw["workQueue"] if w["tenantId"] == journey.tenant_id]
    assert sum(w["count"] for w in mine) == 3
    assert sum(w["over30m"] for w in mine) == 1
    oldest = min(datetime.fromisoformat(w["oldestAtUtc"]) for w in mine)
    assert timedelta(minutes=39) < datetime.now(UTC) - oldest < timedelta(minutes=41)
