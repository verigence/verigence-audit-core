"""Every task has an end the PC can reach (product owner 2026-11-15); on a real Postgres."""
from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_tasks import TaskStateError, submit_action
from audit_core.uc03_task_closure import run_closure_watchdog


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine)
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _task(journey, *, status: str = "READY", source_code: str = "DOCUMENT_MISSING", task_type: str = "DOCUMENT_MISSING",
          actions: list[str] | None = None) -> UUID:
    return UUID(str(_sql(
        journey,
        "INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, source_type, "
        "source_code, dedupe_key, title, description, reference, severity, priority, assigned_role_code, "
        "allowed_actions, completion_protocol, task_status) VALUES (:t, :j, :type, 'DOCUMENT_COLLECTION', 'SYSTEM', "
        "'DOCUMENT', :code, :k, 'A task', 'x', '{}', 'HIGH', 'HIGH', 'PC', CAST(:actions AS jsonb), "
        "'MACHINE_VERIFIED', :status) RETURNING task_id",
        type=task_type, code=source_code, k=f"k:{uuid4()}", status=status,
        actions=json.dumps(actions or ["UPLOAD_DOCUMENT", "ADD_COMMENT"]),
    ).scalar_one()))


def _act(journey, task_id, action, *, role="PC", comment=None):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return submit_action(connection, tenant_id=journey.tenant_id, task_id=task_id, action=action,
                             actor_id=str(uuid4()), actor_role_code=role, comment=comment, details={})


def _row(journey, task_id):
    return _sql(journey, "SELECT task_status, assigned_role_code, allowed_actions, completion_result "
                         "FROM auditcore.p2_tasks WHERE tenant_id=:t AND task_id=:id", id=task_id).mappings().one()


def test_a_returned_task_goes_to_the_team_lead_with_the_pcs_reason_and_the_tl_closes_it(journey):
    task_id = _task(journey, status="RETURNED")
    with pytest.raises(ValueError, match="10 characters"):
        _act(journey, task_id, "ESCALATE_TO_TL", comment="ok")
    _act(journey, task_id, "ESCALATE_TO_TL", comment="Pawan Gupta is the owner of Shree Shyaam Enterprises")
    row = _row(journey, task_id)
    assert row["assigned_role_code"] == "TL" and row["task_status"] == "READY"
    assert "ACCEPT_EXCEPTION" in row["allowed_actions"]
    _act(journey, task_id, "ACCEPT_EXCEPTION", role="TL", comment="Checked the GST certificate: same owner")
    assert _row(journey, task_id)["task_status"] == "VERIFIED_COMPLETE"


def test_a_wrong_name_task_can_be_sent_at_once_but_others_only_after_a_failed_recheck(journey):
    name_task = _task(journey, source_code="WRONG_CUSTOMER_NAME")
    _act(journey, name_task, "ESCALATE_TO_TL", comment="Same person, the surname is spelt differently")
    assert _row(journey, name_task)["assigned_role_code"] == "TL"
    other = _task(journey)
    with pytest.raises(TaskStateError):
        _act(journey, other, "ESCALATE_TO_TL", comment="I have uploaded it already twice")


def test_a_task_hanging_in_verifying_goes_back_to_the_pc(journey):
    task_id = _task(journey, status="VERIFYING")
    _sql(journey, "UPDATE auditcore.p2_tasks SET updated_at_utc = now() - interval '2 hours' WHERE task_id=:id",
         id=task_id)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert run_closure_watchdog(connection, tenant_id=journey.tenant_id)["verifyingReturned"] == 1
    assert _row(journey, task_id)["task_status"] == "RETURNED"
