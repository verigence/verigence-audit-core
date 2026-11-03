"""RULES_RERUN: every booking's checks run again after a rule changes (2026-11-03)."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_rules_rerun import run_rules_rerun


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="rrr")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _queued(journey) -> list[str]:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return [r[0] for r in c.execute(
            text("SELECT work_key FROM auditcore.p2_work_queue WHERE tenant_id=:t AND journey_id=:j "
                 "AND work_type='CONTROL_EVALUATE' ORDER BY work_key"),
            {"t": journey.tenant_id, "j": journey.journey_id}).all()]


def test_the_dry_run_counts_and_the_commit_queues_the_forced_checks(journey) -> None:
    dry = run_rules_rerun(journey.engine, f"{journey.tenant_id}:rules-1103", [])
    assert dry["commit"] is False and dry["bookings"] >= 1 and dry["queued"] == 0 and _queued(journey) == []
    done = run_rules_rerun(journey.engine, f"{journey.tenant_id}:rules-1103-commit", [])
    assert done["queued"] == done["bookings"] and done["failed"] == 0
    assert _queued(journey) == [f"unit:{journey.journey_id}:NATIVE:BOOKING"]


def test_unknown_references_are_listed(journey) -> None:
    assert run_rules_rerun(journey.engine, f"{journey.tenant_id}:x", ["NOPE-1"])["notFound"] == ["NOPE-1"]
