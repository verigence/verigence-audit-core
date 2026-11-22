"""Pages that were never sent to be read (22 Nov 2026, product owner: "how can a file wait for
1 hour ... we need to introspect and do regression and fix").

Found on Mahindra Orissa: four documents of one Journey sat QUEUED for an hour and were failed
"Not read within 30 minutes" without ever being picked up; 109 the day before, in 20 Journeys.
Two faults fed each other:

1. While a Journey's pages are being read, a check's task is held back and a follow-up recompute
   is asked for "in 10 minutes" (16 Nov). Asked from inside the running recompute, the delay was
   dropped: the worker took it as "asked again while it ran" and ran it again at once -- every
   7 seconds, for as long as a page was still waiting.
2. A page waited for every older item of its Journey, also for checks that could not take their
   turn because the recompute goes ahead of them. So the pages were never sent, the reading never
   settled, and the recompute never stopped -- until the time limit failed the pages.

These hold both ends: work asked for later is run later, whoever asks; and a page waits only for
the Journey item that is actually next, never for one stuck behind it.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_p2_task_producer as producer
from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_runtime import enqueue_work


@pytest.fixture
def journey(monkeypatch):
    engine = database_engine()
    created = create_p2_journey(engine, prefix="starve")
    monkeypatch.setattr(worker, "_active_tenants", lambda _engine: [created.tenant_id])
    monkeypatch.setattr(worker, "_maybe_sweep", lambda *_a, **_k: None)
    try:
        yield created
    finally:
        worker._inflight.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _ask(journey, work_type: str, *, key: str | None = None, delay: int = 0, age_seconds: int = 0) -> str:
    key = key or f"test:{uuid4().hex}"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type=work_type, work_key=key, payload={}, correlation_id=None, delay_seconds=delay)
        if age_seconds:
            connection.execute(
                text("UPDATE auditcore.p2_work_queue SET queued_at_utc = now() - make_interval(secs => :s) "
                     "WHERE tenant_id=:t AND work_key=:k"),
                {"t": journey.tenant_id, "k": key, "s": age_seconds})
    return key


def _claim(journey) -> list[worker.WorkItem]:
    return worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)


def _row(journey, key: str):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT work_status, next_attempt_at_utc IS NULL AS due_now, "
                 "EXTRACT(EPOCH FROM next_attempt_at_utc - now()) AS due_in "
                 "FROM auditcore.p2_work_queue WHERE tenant_id=:t AND work_key=:k"),
            {"t": journey.tenant_id, "k": key}).mappings().one()


# ── 1. Work asked for later is run later, also when the running item asks ──────────────────
def test_a_follow_up_asked_for_while_the_item_runs_waits_its_ten_minutes(journey):
    key = _ask(journey, "STAGE_RECOMPUTE", key=f"booking:{journey.journey_id}")
    [running] = _claim(journey)
    # What the recompute itself does when it holds a task back during the reading.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        producer._follow_up_when_settled(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    worker._complete(journey.engine, running)
    row = _row(journey, key)
    assert row["work_status"] == "PENDING"
    assert 500 < float(row["due_in"]) <= 600, "the follow-up ran at once instead of in ten minutes"
    assert [w.work_type for w in _claim(journey)] != ["STAGE_RECOMPUTE"]


def test_asked_again_for_now_while_it_runs_still_runs_again_at_once(journey):
    key = _ask(journey, "STAGE_RECOMPUTE", key=f"booking:{journey.journey_id}")
    [running] = _claim(journey)
    _ask(journey, "STAGE_RECOMPUTE", key=key, delay=600)   # a follow-up for later ...
    _ask(journey, "STAGE_RECOMPUTE", key=key)              # ... and a fact change that needs it now
    worker._complete(journey.engine, running)
    assert [w.work_key for w in _claim(journey)] == [key]


def test_a_later_follow_up_does_not_put_off_work_that_is_due_now(journey):
    key = _ask(journey, "STAGE_RECOMPUTE", key=f"booking:{journey.journey_id}")
    _ask(journey, "STAGE_RECOMPUTE", key=key, delay=600)
    assert _row(journey, key)["due_now"], "a follow-up in ten minutes put off a recompute that was due"
    assert [w.work_key for w in _claim(journey)] == [key]


def test_the_earlier_of_two_later_times_is_kept(journey):
    key = _ask(journey, "STAGE_RECOMPUTE", key=f"booking:{journey.journey_id}", delay=600)
    _ask(journey, "STAGE_RECOMPUTE", key=key, delay=60)
    assert 0 < float(_row(journey, key)["due_in"]) <= 60
    _ask(journey, "STAGE_RECOMPUTE", key=key, delay=600)
    assert 0 < float(_row(journey, key)["due_in"]) <= 60


def test_an_item_whose_worker_died_is_taken_again_whatever_was_asked_meanwhile(journey):
    key = _ask(journey, "STAGE_RECOMPUTE", key=f"booking:{journey.journey_id}")
    _claim(journey)
    _ask(journey, "STAGE_RECOMPUTE", key=key, delay=600)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_work_queue SET lease_expires_at_utc = now() - interval '1 second' "
                                "WHERE tenant_id=:t AND work_key=:k"), {"t": journey.tenant_id, "k": key})
    worker._inflight.clear()
    assert [w.work_key for w in _claim(journey)] == [key]


def test_a_step_that_waits_and_was_asked_for_later_keeps_the_sooner_time(journey):
    key = _ask(journey, "JOURNEY_RECONCILE", key=str(journey.journey_id))
    [running] = _claim(journey)
    _ask(journey, "JOURNEY_RECONCILE", key=key, delay=600)
    worker._reschedule(journey.engine, running, worker.RescheduleWork("waiting", delay_seconds=5))
    assert 0 < float(_row(journey, key)["due_in"]) <= 5


# ── 2. A page waits only for the Journey item that is actually next ────────────────────────
def test_a_page_is_sent_although_older_checks_wait_behind_a_recompute(journey, monkeypatch):
    """22 Nov, live: the checks were asked for first, the PC's pages came next, and the recompute
    (asked for again, so at the back of the line) went ahead of the checks every time."""
    monkeypatch.setattr(worker, "_PER_JOURNEY_CONCURRENCY", 6)
    check = _ask(journey, "CONTROL_EVALUATE", key=f"unit:{journey.journey_id}:NATIVE:BOOKING", age_seconds=60)
    pages = [_ask(journey, "DOCUMENT_INGEST", age_seconds=30 - n) for n in range(4)]
    recompute = _ask(journey, "STAGE_RECOMPUTE", key=f"booking:{journey.journey_id}")
    claimed = [w.work_key for w in _claim(journey)]
    assert sorted(claimed) == sorted(pages), "the pages waited behind checks that could not take their turn"
    assert _claim(journey) == []          # the Journey's own work waits while its pages are sent
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_work_queue SET work_status='COMPLETED' "
                                "WHERE tenant_id=:t AND work_key = ANY(:k)"), {"t": journey.tenant_id, "k": pages})
    worker._inflight.clear()
    assert [w.work_key for w in _claim(journey)] == [recompute]   # then the recompute, then the checks
    assert check not in claimed


def test_a_recompute_that_keeps_coming_back_never_holds_a_page(journey, monkeypatch):
    """Whatever asks for a recompute again and again, each time it joins the back of the line:
    a page that was already waiting goes first."""
    monkeypatch.setattr(worker, "_PER_JOURNEY_CONCURRENCY", 6)
    key = f"booking:{journey.journey_id}"
    _ask(journey, "CONTROL_EVALUATE", key=f"unit:{journey.journey_id}:NATIVE:BOOKING", age_seconds=60)
    _ask(journey, "STAGE_RECOMPUTE", key=key, age_seconds=50)
    [first] = _claim(journey)
    assert first.work_key == key          # asked for before the page: it runs first, once
    page = _ask(journey, "DOCUMENT_INGEST")
    _ask(journey, "STAGE_RECOMPUTE", key=key)   # asked again while it runs
    worker._complete(journey.engine, first)
    worker._inflight.clear()
    assert [w.work_key for w in _claim(journey)] == [page]


def test_a_journey_item_asked_for_first_still_runs_before_a_later_page(journey):
    check = _ask(journey, "CONTROL_EVALUATE", key=f"unit:{journey.journey_id}:NATIVE:BOOKING", age_seconds=30)
    _ask(journey, "DOCUMENT_INGEST")
    assert [w.work_key for w in _claim(journey)] == [check]
    assert _claim(journey) == []          # and the page never runs beside it


def test_only_as_many_pages_as_allowed_run_side_by_side_whatever_else_waits(journey, monkeypatch):
    monkeypatch.setattr(worker, "_PER_JOURNEY_CONCURRENCY", 2)
    _ask(journey, "CONTROL_EVALUATE", key=f"unit:{journey.journey_id}:NATIVE:BOOKING", age_seconds=60)
    pages = [_ask(journey, "DOCUMENT_INGEST", age_seconds=30 - n) for n in range(5)]
    _ask(journey, "STAGE_RECOMPUTE", key=f"booking:{journey.journey_id}")
    assert sorted(w.work_key for w in _claim(journey)) == sorted(pages[:2])
    assert _claim(journey) == []


# ── 3. The whole of it, as it ran live ─────────────────────────────────────────────────────
def test_the_worker_sends_the_pages_and_runs_the_recompute_once(journey, monkeypatch):
    """The 22 Nov Journey, through the running worker: checks asked for first, the PC's four pages
    next, and a recompute that -- each time it runs while a page is still unread -- holds a task
    back and asks for its follow-up. Before: the recompute ran without end and no page was sent."""
    import threading

    ran: list[str] = []
    unread = {"pages": 4}

    def process(engine, work):
        ran.append(work.work_type)
        if work.work_type == "DOCUMENT_INGEST":
            unread["pages"] -= 1
        elif work.work_type in ("STAGE_RECOMPUTE", "CONTROL_EVALUATE") and unread["pages"] > 0:
            with engine.begin() as connection:      # uc03_p2_task_producer.raise_or_refresh, "DEFERRED"
                set_tenant_context(connection, work.tenant_id)
                producer._follow_up_when_settled(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)

    monkeypatch.setattr(worker, "process_work", process)
    monkeypatch.setattr(worker, "_POLL_SECONDS", 0.05)
    _ask(journey, "CONTROL_EVALUATE", key=f"unit:{journey.journey_id}:NATIVE:BOOKING", age_seconds=60)
    _ask(journey, "STAGE_RECOMPUTE", key=f"booking:{journey.journey_id}", age_seconds=50)
    for n in range(4):
        _ask(journey, "DOCUMENT_INGEST", age_seconds=30 - n)

    worker.run_forever(journey.engine, stop=threading.Event(), max_idle_cycles=5)

    assert ran.count("DOCUMENT_INGEST") == 4, f"pages were not sent: {ran}"
    assert ran.count("STAGE_RECOMPUTE") == 1, f"the recompute ran {ran.count('STAGE_RECOMPUTE')} times"
    assert ran.index("STAGE_RECOMPUTE") < ran.index("DOCUMENT_INGEST")   # it was asked for first
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        follow_up = connection.execute(
            text("SELECT work_status, EXTRACT(EPOCH FROM next_attempt_at_utc - now()) FROM auditcore.p2_work_queue "
                 "WHERE tenant_id=:t AND work_type='STAGE_RECOMPUTE'"), {"t": journey.tenant_id}).one()
    assert follow_up[0] == "PENDING" and 500 < float(follow_up[1]) <= 600   # the follow-up, in ten minutes
