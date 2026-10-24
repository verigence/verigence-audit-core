"""A one-shot SKU run the platform killed half way through resumes; it is not
abandoned (real Postgres).

Found 2026-10-24, after it happened twice in one morning: deploying the worker
replaces the container, and a run in flight dies with it. The guard against
repeating a run read "a run row exists for this tag", but that row is created
when the run STARTS -- so the next container logged "sku_run_already_done" and
skipped the rest of the Journeys for good. The log said the run was done when
most of it had never happened, which is the worst kind of wrong: a report that
flatters itself.

A run is done only when it says COMPLETE. Anything else resumes from the
Journey it reached, which is exact because the run pages in a stable order
(created_at_utc, journey_id).
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_backfill_document_sync_producers as backfill
from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context

TAG = "resume-test-20261024"


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="skurs")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _run_row(journey, *, status: str, items: int):
    """A run of this tag that reached ``items`` Journeys and then stopped."""
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        run_id = connection.execute(
            text("INSERT INTO auditcore.sku_resolution_runs "
                 "(tenant_id, started_by_actor_id, journeys_total, run_mode, status) "
                 "VALUES (:t, :a, 40, 'RESOLVE', :s) RETURNING run_id"),
            {"t": journey.tenant_id, "a": f"worker:{TAG}"[:120], "s": status},
        ).scalar_one()
        for _ in range(items):
            connection.execute(
                text("INSERT INTO auditcore.sku_resolution_run_items "
                     "(tenant_id, run_id, journey_id, outcome) VALUES (:t, :r, :j, 'STILL_UNMATCHED')"),
                {"t": journey.tenant_id, "r": run_id, "j": uuid4()},
            )
    return run_id


def _capture(monkeypatch, journey):
    """Record what the run driver asks for, and finish on the first page."""
    seen: list[dict] = []

    def fake(engine, *, tenant_id, offset, limit, run_id=None, actor_id=None, only_without_sku=False):
        seen.append({"offset": offset, "run_id": run_id})
        return {"runId": run_id or uuid4(), "nextOffset": None, "journeysTotal": 40,
                "withSkuBefore": 0, "withSkuAfter": 0, "withoutSkuAfter": 0, "journeysFailed": []}

    monkeypatch.setattr(worker, "_active_tenants", lambda engine: [journey.tenant_id])
    monkeypatch.setattr(backfill, "backfill_sku_resolution_for_tenant", fake)
    return seen


def test_a_run_killed_half_way_resumes_from_the_journey_it_reached(journey, monkeypatch):
    run_id = _run_row(journey, status="RUNNING", items=17)
    seen = _capture(monkeypatch, journey)

    worker._sku_run_once(journey.engine, TAG)

    assert len(seen) == 1, "the run must be picked up again, not skipped"
    assert seen[0]["offset"] == 17, "it resumes at the Journey it reached, not at the start"
    assert str(seen[0]["run_id"]) == str(run_id), "and continues the same run rather than opening a new one"


def test_a_completed_run_is_not_run_again(journey, monkeypatch):
    _run_row(journey, status="COMPLETE", items=40)
    seen = _capture(monkeypatch, journey)

    worker._sku_run_once(journey.engine, TAG)

    assert seen == [], "a finished run must never be repeated for the same tag"


def test_a_tag_never_run_before_starts_at_the_beginning(journey, monkeypatch):
    seen = _capture(monkeypatch, journey)

    worker._sku_run_once(journey.engine, TAG)

    assert len(seen) == 1
    assert seen[0]["offset"] == 0 and seen[0]["run_id"] is None
