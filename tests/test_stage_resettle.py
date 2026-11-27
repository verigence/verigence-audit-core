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


def test_only_open_deliveries_are_settled_again_and_only_on_commit(journey) -> None:
    tag = f"{journey.tenant_id}:closure-1127"
    # A booking (no vehicle on file) is not a delivery: nothing to settle.
    assert run_stage_resettle(journey.engine, tag, [])["openDeliveries"] == 0
    add_evidence(journey, di_document_id=uuid4(), document_type_key="gate_pass", process_area="DELIVERY")
    _clear(journey)

    dry = run_stage_resettle(journey.engine, tag, [])
    assert (dry["commit"], dry["openDeliveries"], dry["queued"]) == (False, 1, 0) and _queued(journey) == 0
    done = run_stage_resettle(journey.engine, tag + "-commit", [])
    assert (done["commit"], done["openDeliveries"], done["queued"]) == (True, 1, 1) and _queued(journey) == 1

    # A delivery already closed is left alone.
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("INSERT INTO auditcore.p2_journey_runtime (tenant_id, journey_id, current_stage, "
                       "booking_completion_state, delivery_completion_state) VALUES (:t, :j, 'DELIVERY_COMPLETE', "
                       "'COMPLETE', 'COMPLETE') ON CONFLICT (tenant_id, journey_id) DO UPDATE SET "
                       "current_stage='DELIVERY_COMPLETE', delivery_completion_state='COMPLETE'"),
                  {"t": journey.tenant_id, "j": journey.journey_id})
    assert run_stage_resettle(journey.engine, tag + "-commit", [])["openDeliveries"] == 0


def test_references_that_are_not_open_deliveries_are_listed(journey) -> None:
    result = run_stage_resettle(journey.engine, f"{journey.tenant_id}:x", ["NOPE-1"])
    assert result["notOpenDeliveries"] == ["NOPE-1"]
