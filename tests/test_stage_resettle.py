"""STAGE_RESETTLE: open deliveries settled again after a closure rule changes (2026-11-27)."""
from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import add_evidence, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_stage_resettle import run_stage_resettle


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="rst")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _queued(journey) -> int:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return int(c.execute(
            text("SELECT count(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND journey_id=:j "
                 "AND work_type='STAGE_RECOMPUTE'"),
            {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one())


def _clear(journey) -> None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("DELETE FROM auditcore.p2_work_queue WHERE tenant_id=:t AND journey_id=:j"),
                  {"t": journey.tenant_id, "j": journey.journey_id})


def _missing_task(journey, status: str = "READY") -> None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, "
                       "source_type, source_code, title, description, dedupe_key, assigned_role_code, task_status, "
                       "completion_protocol) VALUES (:t, :j, 'DOCUMENT_MISSING', 'EVIDENCE_GAP', 'SYSTEM', "
                       "'DOCUMENT', 'DOCUMENT_MISSING', 'Upload the Debit Note', 'x', :k, 'PC', :s, "
                       "'MACHINE_VERIFIED')"),
                  {"t": journey.tenant_id, "j": journey.journey_id, "k": f"document-missing:{journey.journey_id}",
                   "s": status})


def test_open_deliveries_are_settled_again_and_only_on_commit(journey) -> None:
    tag = f"{journey.tenant_id}:closure-1127"
    # A booking (no vehicle on file) with no missing-document task: nothing to settle.
    assert run_stage_resettle(journey.engine, tag, [])["journeys"] == 0
    add_evidence(journey, di_document_id=uuid4(), document_type_key="gate_pass", process_area="DELIVERY")
    _clear(journey)

    dry = run_stage_resettle(journey.engine, tag, [])
    assert (dry["commit"], dry["openDeliveries"], dry["queued"]) == (False, 1, 0) and _queued(journey) == 0
    done = run_stage_resettle(journey.engine, tag + "-commit", [])
    assert (done["commit"], done["openDeliveries"], done["queued"]) == (True, 1, 1) and _queued(journey) == 1

    # A delivery already closed is left alone ...
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("INSERT INTO auditcore.p2_journey_runtime (tenant_id, journey_id, current_stage, "
                       "booking_completion_state, delivery_completion_state) VALUES (:t, :j, 'DELIVERY_COMPLETE', "
                       "'COMPLETE', 'COMPLETE') ON CONFLICT (tenant_id, journey_id) DO UPDATE SET "
                       "current_stage='DELIVERY_COMPLETE', delivery_completion_state='COMPLETE'"),
                  {"t": journey.tenant_id, "j": journey.journey_id})
    assert run_stage_resettle(journey.engine, tag + "-commit", [])["journeys"] == 0
    # ... unless a missing-document task is open on it: settling rebuilds the task from the
    # documents now mandatory (product owner 2026-11-27: the debit note tasks are to close).
    _missing_task(journey)
    again = run_stage_resettle(journey.engine, tag + "-commit", [])
    assert (again["journeys"], again["openDeliveries"], again["withMissingDocumentTask"]) == (1, 0, 1)
    # ... and a Journey named is settled whatever its state.
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        reference = c.execute(text("SELECT journey_reference FROM auditcore.journeys WHERE tenant_id=:t "
                                   "AND journey_id=:j"), {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one()
        c.execute(text("UPDATE auditcore.p2_tasks SET task_status='VERIFIED_COMPLETE' WHERE tenant_id=:t "
                       "AND journey_id=:j"), {"t": journey.tenant_id, "j": journey.journey_id})
    assert run_stage_resettle(journey.engine, tag, [])["journeys"] == 0
    assert run_stage_resettle(journey.engine, tag, [reference])["journeys"] == 1


def test_unknown_references_are_listed(journey) -> None:
    assert run_stage_resettle(journey.engine, f"{journey.tenant_id}:x", ["NOPE-1"])["notFound"] == ["NOPE-1"]
