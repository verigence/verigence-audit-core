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


def _enqueue(journey, n: int, work_type: str = "DOCUMENT_INGEST") -> list[str]:
    """Page work: several of one Journey may run at once (journey work runs alone)."""
    keys = [f"test:{uuid4().hex}" for _ in range(n)]
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        for key in keys:
            worker._enqueue(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                            work_type=work_type, work_key=key, payload={}, correlation_id=None)
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


# ── one Journey's work (2026-10-28: 881 deadlocks between a Journey's own items) ──
def _claim_keys(journey) -> list[str]:
    return [w.work_key for w in worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)]


def test_work_on_the_journey_as_a_whole_runs_alone(journey):
    checks = _enqueue(journey, 3, work_type="CONTROL_EVALUATE")
    reconcile = _enqueue(journey, 1, work_type="JOURNEY_RECONCILE")
    claimed = _claim_keys(journey)
    assert len(claimed) == 1 and set(claimed) <= set(checks + reconcile)
    assert _claim_keys(journey) == []  # the next waits until the first is done


def test_page_work_runs_side_by_side_up_to_the_limit(journey, monkeypatch):
    monkeypatch.setattr(worker, "_PER_JOURNEY_CONCURRENCY", 3)
    pages = _enqueue(journey, 5)
    claimed = _claim_keys(journey)
    assert len(claimed) == 3 and set(claimed) <= set(pages)


def test_page_work_and_journey_work_never_overlap(journey, monkeypatch):
    monkeypatch.setattr(worker, "_PER_JOURNEY_CONCURRENCY", 3)
    first = _enqueue(journey, 1)
    check = _enqueue(journey, 1, work_type="CONTROL_EVALUATE")
    later = _enqueue(journey, 1)
    assert _claim_keys(journey) == first  # the check waits for the page before it
    assert _claim_keys(journey) == []     # the later page waits for the check
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_work_queue SET work_status='COMPLETED' "
                                "WHERE tenant_id=:t AND work_key=:k"), {"t": journey.tenant_id, "k": first[0]})
    assert _claim_keys(journey) == check
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_work_queue SET work_status='COMPLETED' "
                                "WHERE tenant_id=:t AND work_key=:k"), {"t": journey.tenant_id, "k": check[0]})
    assert _claim_keys(journey) == later
