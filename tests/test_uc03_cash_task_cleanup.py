"""Cash transactions raise no tasks (product owner, 2026-10-31): the task
producer's suppression point and the CASH_TASK_CLEANUP job that cancels the
tasks raised before."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_cash_task_cleanup import clean_tenant, run_cash_task_cleanup
from audit_core.uc03_p2_controls import write_control_state
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_task_producer import (
    SUPPRESSED_TASK_CODES,
    _json,
    apply_control_transitions,
    raise_or_refresh,
)
from audit_core.uc03_p2_worker import _one_shot_target


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2cash")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _open_task(journey, code: str, *, task_type: str = "FINDING_REVIEW", status: str = "READY",
               owner: str = "TL") -> str:
    template = get_registry().tasks[task_type]
    return str(_sql(
        journey,
        "INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, source_type, "
        "source_code, dedupe_key, title, description, reference, severity, priority, assigned_role_code, "
        "allowed_actions, completion_protocol, task_status) VALUES (:t, :j, :type, :cat, 'SYSTEM', 'RULE', :code, "
        ":key, :title, 'raised before 2026-10-31', '{}'::jsonb, 'HIGH', 'HIGH', :owner, CAST(:actions AS jsonb), "
        "'MACHINE_VERIFIED', :status) RETURNING task_id",
        type=task_type, cat=template.category, code=code, key=f"control:{journey.journey_id}:{code}",
        title=f"{code} task", owner=owner, actions=_json(list(template.actions)), status=status,
    ).scalar_one())


def _finding(journey, code: str) -> str:
    return str(_sql(
        journey,
        "INSERT INTO auditcore.audit_findings (tenant_id, journey_id, finding_type_code, severity, finding_status, "
        "title, description, stage_code, origin_kind, origin_role_snapshot, rule_key, blocking_completion, "
        "finding_class) VALUES (:t, :j, 'PAYMENT_EXCEPTION', 'HIGH', 'OPEN', :title, 'x', 'BOOKING', 'MACHINE', "
        "'SYSTEM', :code, FALSE, 'VIOLATION') RETURNING audit_finding_id",
        title=f"{code} finding", code=code,
    ).scalar_one())


def _state(journey, code: str, status: str = "FAIL") -> None:
    _sql(journey, "INSERT INTO auditcore.p2_control_state (tenant_id, journey_id, control_code, executor_type, "
                  "control_mode, control_status, stage_code) VALUES (:t, :j, :c, 'NATIVE', 'RERUN', :s, 'BOOKING')",
         c=code, s=status)


def _tasks(journey) -> dict[str, dict]:
    rows = _sql(journey, "SELECT task_id, source_code, task_status, completion_result FROM auditcore.p2_tasks "
                         "WHERE tenant_id=:t AND journey_id=:j").mappings().all()
    return {str(r["source_code"]): dict(r) for r in rows}


# ------------------------------------------------------- the suppression


def test_the_four_cash_checks_are_suppressed():
    assert SUPPRESSED_TASK_CODES == {"CASH_INTIMATION_UNCONFIRMED", "CASH_NOT_INTIMATED", "CASH_ABOVE_LIMIT",
                                     "PAYMENT_MODE_CASH_UNRECEIPTED"}
    controls = get_registry().controls
    # The question and its finding are retired; the other two still run.
    assert "CASH_INTIMATION_UNCONFIRMED" not in controls and "CASH_NOT_INTIMATED" not in controls
    assert controls["CASH_ABOVE_LIMIT"].finding_class == "VIOLATION"
    assert controls["PAYMENT_MODE_CASH_UNRECEIPTED"].mode == "EXTERNAL"


def test_a_failing_rule_engine_cash_check_raises_no_task(journey):
    registry = get_registry()
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        transition = write_control_state(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            control=registry.controls["PAYMENT_MODE_CASH_UNRECEIPTED"], status="FAIL", stage="BOOKING",
            reason="Cash credited on the ledger is not receipted",
            details={"leftValue": "50000", "rightValue": "0"},
        )
        counts = apply_control_transitions(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, transitions=[transition],
            evaluation_started_at=datetime.now(UTC),
        )
    assert counts == {"SUPPRESSED": 1}
    assert _tasks(journey) == {}
    # The check's result stays on the ledger.
    assert _sql(journey, "SELECT control_status FROM auditcore.p2_control_state WHERE tenant_id=:t AND journey_id=:j "
                         "AND control_code='PAYMENT_MODE_CASH_UNRECEIPTED'").scalar_one() == "FAIL"


def test_raise_or_refresh_never_raises_a_cash_task(journey):
    """Whoever calls it: nothing is raised, and a task left open is cancelled."""
    def call():
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            return raise_or_refresh(
                connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                dedupe_key=f"control:{journey.journey_id}:CASH_ABOVE_LIMIT", task_type="FINDING_REVIEW",
                source_type="RULE", source_code="CASH_ABOVE_LIMIT", title="Cash receipt above the statutory limit",
                description="x", reference={}, severity="HIGH", registry=get_registry(),
            )

    assert call() == (None, "SUPPRESSED")
    assert _tasks(journey) == {}
    task_id = _open_task(journey, "CASH_ABOVE_LIMIT", status="VERIFYING")
    cancelled, outcome = call()
    assert (str(cancelled), outcome) == (task_id, "SUPPRESSED")
    task = _tasks(journey)["CASH_ABOVE_LIMIT"]
    assert task["task_status"] == "CANCELLED" and task["completion_result"]["outcome"] == "CHECK_RETIRED"
    assert call() == (None, "SUPPRESSED")


# --------------------------------------------------------------- the job


def test_cash_task_cleanup_dry_run_then_commit(journey):
    for code, task_type, owner in (("CASH_INTIMATION_UNCONFIRMED", "PC_CONFIRMATION", "PC"),
                                   ("CASH_NOT_INTIMATED", "FINDING_REVIEW", "TL"),
                                   ("CASH_ABOVE_LIMIT", "FINDING_REVIEW", "TL"),
                                   ("PAYMENT_MODE_CASH_UNRECEIPTED", "RULE_DISCREPANCY_REVIEW", "PC")):
        _open_task(journey, code, task_type=task_type, owner=owner,
                   status="IN_PROGRESS" if code == "CASH_ABOVE_LIMIT" else "READY")
    _open_task(journey, "THIRD_PARTY_PAYMENT_UNCONFIRMED", task_type="PC_CONFIRMATION", owner="PC")
    # A cash task already closed is left as it is.
    _sql(journey, "UPDATE auditcore.p2_tasks SET dedupe_key = dedupe_key || '-old', task_status='VERIFIED_COMPLETE' "
                  "WHERE tenant_id=:t AND source_code='CASH_NOT_INTIMATED'")
    _open_task(journey, "CASH_NOT_INTIMATED")
    not_intimated = _finding(journey, "CASH_NOT_INTIMATED")
    above_limit = _finding(journey, "CASH_ABOVE_LIMIT")
    _state(journey, "CASH_INTIMATION_UNCONFIRMED")
    _state(journey, "CASH_NOT_INTIMATED")
    _state(journey, "CASH_ABOVE_LIMIT")

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        dry = clean_tenant(connection, tenant_id=journey.tenant_id, tag="t1", commit=False)
    assert dry["tasks"] == 4 and dry["journeys"] == 1
    assert dry["tasksByCode"] == {code: 1 for code in SUPPRESSED_TASK_CODES}
    assert dry["findings"] == 1 and dry["controlStateRows"] == 2
    assert dry["cancelled"] == 0
    assert {c: t["task_status"] for c, t in _tasks(journey).items()}["CASH_ABOVE_LIMIT"] == "IN_PROGRESS"

    run_cash_task_cleanup(journey.engine, "t1-commit", [journey.tenant_id])

    rows = _sql(journey, "SELECT task_id, source_code, task_status, completion_result FROM auditcore.p2_tasks "
                         "WHERE tenant_id=:t AND journey_id=:j AND task_status <> 'VERIFIED_COMPLETE'").mappings().all()
    by_code = {str(r["source_code"]): dict(r) for r in rows}
    for code in SUPPRESSED_TASK_CODES:
        assert by_code[code]["task_status"] == "CANCELLED", code
        assert by_code[code]["completion_result"]["outcome"] == "CHECK_RETIRED"
        assert by_code[code]["completion_result"]["reason"] == (
            "Cash transactions raise no tasks (product owner, 2026-10-31)")
        events = _sql(journey, "SELECT event_type, details FROM auditcore.p2_task_events "
                               "WHERE tenant_id=:t AND task_id=:id", id=by_code[code]["task_id"]).mappings().all()
        assert [e["event_type"] for e in events] == ["CANCELLED"]
        assert events[0]["details"]["job"] == "CASH_TASK_CLEANUP"
    assert by_code["THIRD_PARTY_PAYMENT_UNCONFIRMED"]["task_status"] == "READY"
    assert _sql(journey, "SELECT count(*) FROM auditcore.p2_tasks WHERE tenant_id=:t "
                         "AND task_status='VERIFIED_COMPLETE'").scalar_one() == 1
    statuses = dict(_sql(journey, "SELECT audit_finding_id::text, finding_status FROM auditcore.audit_findings "
                                  "WHERE tenant_id=:t").all())
    assert statuses == {not_intimated: "OPEN", above_limit: "OPEN"}  # findings are kept
    states = _sql(journey, "SELECT control_code FROM auditcore.p2_control_state WHERE tenant_id=:t").scalars().all()
    assert len(states) == 3  # left in place: the runtime role may not delete them, and nothing reads them

    # Idempotent: nothing left to do.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        again = clean_tenant(connection, tenant_id=journey.tenant_id, tag="t2-commit", commit=True)
    assert (again["tasks"], again["cancelled"]) == (0, 0)


def test_cash_task_cleanup_is_a_worker_job(journey):
    target, args = _one_shot_target("CASH_TASK_CLEANUP", engine=journey.engine, tag="x", reports=[])
    assert target is run_cash_task_cleanup
    assert args[:2] == (journey.engine, "x")


def test_a_project_without_cash_tasks_is_untouched(journey):
    _open_task(journey, "THIRD_PARTY_PAYMENT_UNCONFIRMED", task_type="PC_CONFIRMATION", owner="PC")
    run_cash_task_cleanup(journey.engine, "t-commit", [journey.tenant_id])
    assert _tasks(journey)["THIRD_PARTY_PAYMENT_UNCONFIRMED"]["task_status"] == "READY"
