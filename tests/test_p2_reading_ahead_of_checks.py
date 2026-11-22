"""Pages Document Intelligence had read, failed "Not read within 30 minutes" (22 Nov 2026,
product owner: "we need to introspect and do regression and fix").

Found on Mahindra Orissa, in the pages failed by the time limit over 11 days:

- 12 Nov, 3:10 pm: a rules re-run asked for every booking's checks again -- some 33,000 items
  in the worker's one line. A page is read when it is sent, and when its result is fetched
  (JOURNEY_RECONCILE, the look at Document Intelligence). The sending went ahead of checks
  since 29 Oct; the look stood in the checks' line. For 95 minutes no result was fetched:
  136 pages were failed although Document Intelligence was working. The same after the
  re-runs of 14 Nov (114 pages), 16 Nov and 21 Nov.
- A page uploaded to a booking whose own checks were part of that re-run waited for them, and
  they waited for their turn in the long line.

These hold the order: reading -- the page, the look, and whatever of its own booking stands
before them -- is taken before the checks of bookings with nothing to read.
"""
from __future__ import annotations

import threading
import time
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_runtime import enqueue_work


@pytest.fixture
def journey(monkeypatch):
    engine = database_engine()
    created = create_p2_journey(engine, prefix="rerun")
    monkeypatch.setattr(worker, "_active_tenants", lambda _engine: [created.tenant_id])
    monkeypatch.setattr(worker, "_maybe_sweep", lambda *_a, **_k: None)
    try:
        yield created
    finally:
        worker._inflight.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _another_booking(journey) -> UUID:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return UUID(str(connection.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference) "
                 "VALUES (:t, :d, :o, :c, :r) RETURNING journey_id"),
            {"t": journey.tenant_id, "d": journey.dealer_id, "o": journey.outlet_id, "c": journey.customer_id,
             "r": f"RERUN-{uuid4().hex[:10]}"}).scalar_one()))


def _ask(journey, work_type: str, *, journey_id: UUID | None = None, key: str | None = None,
         age_seconds: int = 0) -> str:
    journey_id = journey_id or journey.journey_id
    key = key or f"test:{uuid4().hex}"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey_id,
                     work_type=work_type, work_key=key, payload={}, correlation_id=None)
        if age_seconds:
            connection.execute(
                text("UPDATE auditcore.p2_work_queue SET queued_at_utc = now() - make_interval(secs => :s) "
                     "WHERE tenant_id=:t AND work_key=:k"),
                {"t": journey.tenant_id, "k": key, "s": age_seconds})
    return key


def _rules_rerun(journey, bookings: int, *, checks_each: int = 1, age_seconds: int = 600) -> list[str]:
    """What RULES_RERUN leaves in the line: every booking's checks, asked for at one time."""
    keys = []
    for _ in range(bookings):
        other = _another_booking(journey)
        for n in range(checks_each):
            keys.append(_ask(journey, "CONTROL_EVALUATE", journey_id=other,
                             key=f"unit:{other}:NATIVE:{n}", age_seconds=age_seconds))
    return keys


def _claim(journey, limit: int = 5) -> list[worker.WorkItem]:
    return worker._claim_for_tenant(journey.engine, journey.tenant_id, limit)


def _done(journey, items) -> None:
    for item in items:
        worker._complete(journey.engine, item)
    worker._inflight.clear()


# ── 1. Fetching what was read does not stand in the checks' line ───────────────────────────
def test_the_look_for_what_was_read_is_taken_before_a_rules_rerun(journey):
    """12 Nov: the pages were with Document Intelligence, the look was asked for, and 33,000
    checks asked for earlier were all taken first."""
    _rules_rerun(journey, bookings=12)
    look = _ask(journey, "JOURNEY_RECONCILE", key=str(journey.journey_id))
    claimed = _claim(journey, limit=5)
    assert claimed[0].work_key == look, "the look for what was read waited behind the re-run's checks"


def test_every_booking_being_read_is_looked_at_before_the_rerun_goes_on(journey):
    _rules_rerun(journey, bookings=12)
    looks = [_ask(journey, "JOURNEY_RECONCILE", key=str(journey.journey_id))]
    for _ in range(3):
        other = _another_booking(journey)
        looks.append(_ask(journey, "JOURNEY_RECONCILE", journey_id=other, key=str(other)))
    assert [w.work_key for w in _claim(journey, limit=4)] == looks       # oldest first among them


def test_a_recompute_asked_before_the_look_runs_at_once_so_the_look_can(journey):
    """A Journey's items run one at a time. The recompute the re-run asked for is before the look
    in the booking's own line; it is taken now, not in its turn in the long line."""
    _rules_rerun(journey, bookings=12)
    recompute = _ask(journey, "STAGE_RECOMPUTE", key=f"booking:{journey.journey_id}", age_seconds=300)
    look = _ask(journey, "JOURNEY_RECONCILE", key=str(journey.journey_id))
    first = _claim(journey, limit=1)
    assert [w.work_key for w in first] == [recompute], "what stands before the look waited in the long line"
    _done(journey, first)
    assert [w.work_key for w in _claim(journey, limit=1)] == [look]


# ── 2. A page of a booking that is part of the re-run ──────────────────────────────────────
def test_a_page_is_not_held_for_the_whole_rerun_by_its_own_bookings_checks(journey, monkeypatch):
    """The booking's own checks were asked for by the re-run, before the PC uploaded. They are
    before the page in the booking's line (one item of a Journey at a time): they run now, and
    the page is sent."""
    monkeypatch.setattr(worker, "_PER_JOURNEY_CONCURRENCY", 6)
    _rules_rerun(journey, bookings=12)
    own = [_ask(journey, "CONTROL_EVALUATE", key=f"unit:{journey.journey_id}:NATIVE:{n}", age_seconds=300 - n)
           for n in range(3)]
    page = _ask(journey, "DOCUMENT_INGEST")
    taken: list[str] = []
    for _ in range(4):                       # one worker slot, four turns
        items = _claim(journey, limit=1)
        taken += [w.work_key for w in items]
        _done(journey, items)
    assert taken == [*own, page], "the page waited for the re-run of the other bookings"


def test_pages_still_go_before_everything_else(journey, monkeypatch):
    monkeypatch.setattr(worker, "_PER_JOURNEY_CONCURRENCY", 6)
    _rules_rerun(journey, bookings=6)
    other = _another_booking(journey)
    _ask(journey, "JOURNEY_RECONCILE", journey_id=other, key=str(other), age_seconds=30)
    page = _ask(journey, "DOCUMENT_INGEST")
    assert _claim(journey, limit=1)[0].work_key == page


# ── 3. Nothing else changes place ──────────────────────────────────────────────────────────
def test_checks_of_bookings_with_nothing_to_read_keep_their_order(journey):
    keys = []
    for n in range(6):
        other = _another_booking(journey)
        keys.append(_ask(journey, "CONTROL_EVALUATE", journey_id=other, key=f"unit:{other}:NATIVE:0",
                         age_seconds=600 - 10 * n))
    assert [w.work_key for w in _claim(journey, limit=6)] == keys


def test_a_bookings_checks_do_not_jump_the_line_while_its_look_is_not_due(journey):
    """Between two looks (the look waits a few seconds before it asks again) the booking's
    checks are ordinary checks: they do not go ahead of older ones of other bookings."""
    older = _rules_rerun(journey, bookings=3)
    own = _ask(journey, "CONTROL_EVALUATE", key=f"unit:{journey.journey_id}:NATIVE:0", age_seconds=60)
    look = _ask(journey, "JOURNEY_RECONCILE", key=str(journey.journey_id))
    [running] = [w for w in _claim(journey, limit=1) if w.work_key == look]
    worker._reschedule(journey.engine, running, worker.RescheduleWork("waiting", delay_seconds=30))
    worker._inflight.clear()
    assert [w.work_key for w in _claim(journey, limit=4)] == [*older, own]


def test_one_item_of_a_journey_at_a_time_still(journey):
    _ask(journey, "STAGE_RECOMPUTE", key=f"booking:{journey.journey_id}", age_seconds=300)
    _ask(journey, "JOURNEY_RECONCILE", key=str(journey.journey_id))
    _ask(journey, "CONTROL_EVALUATE", key=f"unit:{journey.journey_id}:NATIVE:0", age_seconds=600)
    assert len(_claim(journey, limit=5)) == 1
    assert _claim(journey, limit=5) == []


# ── 4. The whole of it, as it ran live ─────────────────────────────────────────────────────
def test_the_worker_fetches_results_and_sends_pages_while_a_rerun_is_in_the_line(journey, monkeypatch):
    """12 Nov through the running worker: a re-run of 40 bookings x 5 checks is in the line; a
    booking that is part of it gets a page from the PC and has a look due. Before: the page and
    the look came after the 200 checks (live: after 33,000, 95 minutes)."""
    ran: list[tuple[str, UUID]] = []

    def process(engine, work):
        time.sleep(0.01)
        ran.append((work.work_type, work.journey_id))

    monkeypatch.setattr(worker, "process_work", process)
    monkeypatch.setattr(worker, "_POLL_SECONDS", 0.05)
    monkeypatch.setattr(worker, "_WORKER_CONCURRENCY", 4)
    _rules_rerun(journey, bookings=40, checks_each=5)
    for n in range(5):                                           # this booking's share of the re-run
        _ask(journey, "CONTROL_EVALUATE", key=f"unit:{journey.journey_id}:NATIVE:{n}", age_seconds=600 - n)
    _ask(journey, "STAGE_RECOMPUTE", key=f"booking:{journey.journey_id}", age_seconds=590)
    _ask(journey, "DOCUMENT_INGEST")                             # the PC uploads a page now
    _ask(journey, "JOURNEY_RECONCILE", key=str(journey.journey_id))   # and earlier pages are with DI

    worker.run_forever(journey.engine, stop=threading.Event(), max_idle_cycles=5)

    kinds = [kind for kind, _ in ran]
    assert len(ran) == 40 * 5 + 5 + 3, f"work was lost or run twice: {len(ran)} items"
    # The recompute that stands before the page, the page, then the look: with four slots, at
    # most a dozen of the re-run's checks are taken beside them.
    assert kinds.index("DOCUMENT_INGEST") <= 12, f"the page was sent {kinds.index('DOCUMENT_INGEST')} items in"
    assert kinds.index("JOURNEY_RECONCILE") <= 16, (
        f"the result was fetched {kinds.index('JOURNEY_RECONCILE')} items in")
    assert kinds.index("STAGE_RECOMPUTE") < kinds.index("DOCUMENT_INGEST") < kinds.index("JOURNEY_RECONCILE")
