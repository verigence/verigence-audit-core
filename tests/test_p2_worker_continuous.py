"""Worker restarts and throughput (2026-10-27 review of 26 Oct's "not read within
30 minutes" pages): the worker claims as soon as a slot is free instead of
waiting for the slowest item of a batch, and a stopping worker hands its
unfinished items over at once instead of leaving them locked for the lease."""
from __future__ import annotations

import threading
import time
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context


@pytest.fixture
def journey(monkeypatch):
    engine = database_engine()
    created = create_p2_journey(engine, prefix="cont")
    monkeypatch.setattr(worker, "_active_tenants", lambda _engine: [created.tenant_id])
    monkeypatch.setattr(worker, "_maybe_sweep", lambda *_a, **_k: None)
    try:
        yield created
    finally:
        worker._inflight.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _enqueue(journey, n: int) -> list[str]:
    keys = [f"test:{uuid4().hex}" for _ in range(n)]
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        for key in keys:
            worker._enqueue(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                            work_type="STAGE_RECOMPUTE", work_key=key, payload={}, correlation_id=None)
    return keys


def _rows(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT work_key, work_status, lease_expires_at_utc <= now() AS free FROM auditcore.p2_work_queue "
                 "WHERE tenant_id=:t AND work_key LIKE 'test:%'"), {"t": journey.tenant_id}).mappings().all()


def test_a_slow_item_does_not_hold_back_the_others(journey, monkeypatch):
    """One item takes 2 s; the other five finish long before it, on one pass."""
    keys = _enqueue(journey, 6)
    slow = keys[0]
    finished: list[tuple[str, float]] = []
    start = time.monotonic()

    def process(_engine, work):
        time.sleep(2.0 if work.work_key == slow else 0.05)
        finished.append((work.work_key, time.monotonic() - start))

    monkeypatch.setattr(worker, "process_work", process)
    monkeypatch.setattr(worker, "_PER_JOURNEY_CONCURRENCY", 10)
    monkeypatch.setattr(worker, "_POLL_SECONDS", 0.05)
    worker.run_forever(journey.engine, stop=threading.Event(), max_idle_cycles=3)
    times = dict(finished)
    assert set(times) == set(keys)
    assert max(t for k, t in times.items() if k != slow) < 1.0 < times[slow]
    assert {r["work_status"] for r in _rows(journey)} == {"COMPLETED"}


def test_stopping_hands_unfinished_items_over_at_once(journey, monkeypatch):
    keys = _enqueue(journey, 2)
    hold = threading.Event()
    stop = threading.Event()

    def process(_engine, work):
        stop.set()  # SIGTERM arrives while the items run
        hold.wait(5)

    monkeypatch.setattr(worker, "process_work", process)
    monkeypatch.setattr(worker, "_PER_JOURNEY_CONCURRENCY", 10)
    monkeypatch.setattr(worker, "_DRAIN_SECONDS", 0.2)
    monkeypatch.setattr(worker, "_POLL_SECONDS", 0.05)
    worker.run_forever(journey.engine, stop=stop)
    rows = {r["work_key"]: r for r in _rows(journey)}
    hold.set()
    # Still marked as taken, but the lease is released: the next worker claims them now.
    assert set(rows) == set(keys)
    assert all(r["work_status"] in ("CLAIMED", "PROCESSING") and r["free"] for r in rows.values())
    reclaimed = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    assert {w.work_key for w in reclaimed} == set(keys)
