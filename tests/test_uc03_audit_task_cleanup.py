"""AUDIT_TASK_CLEANUP (2026-11-01): the open check tasks that named no document."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_audit_task_cleanup import FALLBACK, clean_tenant
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_task_producer import _json
from audit_core.uc03_p2_worker import _one_shot_target


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2atc")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _task(journey, code: str, *, key: str | None = None, task_type: str = "FINDING_REVIEW",
          source_type: str = "RULE", description: str = FALLBACK) -> str:
    template = get_registry().tasks[task_type]
    return str(_sql(
        journey,
        "INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, source_type, "
        "source_code, dedupe_key, title, description, reference, severity, priority, assigned_role_code, "
        "allowed_actions, completion_protocol) VALUES (:t, :j, :type, :cat, 'SYSTEM', :st, :code, :key, :title, "
        ":description, '{}'::jsonb, 'HIGH', 'HIGH', 'TL', CAST(:actions AS jsonb), 'MACHINE_VERIFIED') "
        "RETURNING task_id",
        type=task_type, cat=template.category, st=source_type, code=code,
        key=key or f"control:{journey.journey_id}:{code}", title=f"{code} task", description=description,
        actions=_json(list(template.actions)),
    ).scalar_one())


def _status(journey, task_id: str) -> str:
    return _sql(journey, "SELECT task_status FROM auditcore.p2_tasks WHERE tenant_id=:t AND task_id=:id",
                id=task_id).scalar_one()


def test_the_job_is_registered(journey):
    target, args = _one_shot_target("AUDIT_TASK_CLEANUP", journey.engine, "t", [])
    assert target.__name__ == "run_audit_task_cleanup" and args[1] == "t" and journey.tenant_id in args[2]


def test_empty_check_tasks_are_cancelled_only_on_commit_and_only_the_checks_own(journey):
    shells = {code: _task(journey, code) for code in ("WRONG_DOCUMENT", "MANUAL_VERIFICATION", "INVOICE_SKU_MISMATCH")}
    review = _task(journey, "MANUAL_VERIFICATION", key=f"field-review:{journey.journey_id}:x",
                   task_type="MANUAL_VERIFICATION_REVIEW", source_type="DOCUMENT_FIELD", description="Verify 1 field")
    other = _task(journey, "DEAL_UNDERCHARGED", description="Deal charged below the standard price")

    dry = clean_tenant(journey.engine, tenant_id=journey.tenant_id, tag="t", commit=False)
    assert dry["shells"] == 3 and dry["cancelled"] == 0
    assert all(_status(journey, t) == "READY" for t in shells.values())

    done = clean_tenant(journey.engine, tenant_id=journey.tenant_id, tag="t-commit", commit=True)
    assert done["cancelled"] == 3
    assert all(_status(journey, t) == "CANCELLED" for t in shells.values())
    assert _status(journey, review) == "READY" and _status(journey, other) == "READY"
    result = _sql(journey, "SELECT completion_result FROM auditcore.p2_tasks WHERE tenant_id=:t AND task_id=:id",
                  id=shells["WRONG_DOCUMENT"]).scalar_one()
    assert result["outcome"] == "OWN_TASK"
    assert clean_tenant(journey.engine, tenant_id=journey.tenant_id, tag="t-commit", commit=True)["shells"] == 0


def test_a_fallback_duplicate_task_has_its_checks_run_again(journey):
    _task(journey, "DUPLICATE_RECEIPT")
    _task(journey, "DUPLICATE_BOOKING", description="Possible duplicate booking for the same customer")

    def queued():
        return _sql(journey, "SELECT work_key, payload FROM auditcore.p2_work_queue WHERE tenant_id=:t AND journey_id=:j "
                             "AND work_type='CONTROL_EVALUATE'").mappings().all()

    dry = clean_tenant(journey.engine, tenant_id=journey.tenant_id, tag="t", commit=False)
    assert dry["rerunJourneys"] == 1 and dry["rerunByCode"] == {"DUPLICATE_RECEIPT": 1} and queued() == []
    clean_tenant(journey.engine, tenant_id=journey.tenant_id, tag="t-commit", commit=True)
    [unit] = queued()
    assert unit["work_key"].endswith(":NATIVE:BOOKING") and unit["payload"]["force"] is True
