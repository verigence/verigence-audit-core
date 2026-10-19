"""A facts copy that keeps failing must settle the page, not loop for ever.

Found live 2026-10-19: a page whose facts copy failed on a database rule was
treated like "Document Intelligence has not finished": the page never
settled, the Journey reconcile rescheduled itself every five minutes (a
reschedule never counts toward dead-lettering) and held a worker slot for one
to three minutes each round -- 47 rounds across three Journeys in under three
hours, while other Journeys' documents waited.

Now a non-transient failure is counted on the page and, after three in a row,
the page settles as FAILED with a plain reason. A transient failure (DI or
Security unreachable, the database busy) keeps the old "look again shortly".
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import add_evidence, add_page, create_p2_journey, database_engine
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from audit_core import uc03_confidence_review_policy as policy
from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="copyfail")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _work(journey) -> worker.WorkItem:
    return worker.WorkItem(
        tenant_id=journey.tenant_id, work_id=uuid4(), journey_id=journey.journey_id,
        work_type="JOURNEY_RECONCILE", work_key=str(journey.journey_id), payload={},
        attempt_count=0, requested_version=None, correlation_id="t", lease_token=uuid4(),
    )


def _page(journey):
    _batch, queue_id, document_id = add_page(journey, status="SYNCING_TO_AUDIT_CORE")
    add_evidence(journey, di_document_id=document_id, document_type_key="dealer_receipt")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return dict(connection.execute(
            text("SELECT * FROM auditcore.p2_document_queue WHERE tenant_id=:t AND queue_id=:q"),
            {"t": journey.tenant_id, "q": queue_id},
        ).mappings().one())


@pytest.fixture
def failing_sync(monkeypatch: pytest.MonkeyPatch):
    state: dict = {"exc": None}
    monkeypatch.setattr(worker, "get_security_oauth_client", lambda: object())
    monkeypatch.setattr(worker, "get_di_client", lambda: object())
    monkeypatch.setattr(policy, "_prefetch_document_for_sync", lambda *a, **k: SimpleNamespace())

    def sync(*_a, **_k):
        raise state["exc"]

    monkeypatch.setattr(policy, "_sync_booking_document_once", sync)
    return state


def _copy(journey, page):
    return worker._copy_page_facts(
        journey.engine, work=_work(journey), page=page,
        di_item={"classifiedDocumentTypeKey": "dealer_receipt"}, requirement_refs={},
    )


def _attempts(journey, queue_id) -> int:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return int(connection.execute(
            text("SELECT attempt_count FROM auditcore.p2_document_queue WHERE tenant_id=:t AND queue_id=:q"),
            {"t": journey.tenant_id, "q": queue_id},
        ).scalar_one())


def _db_rule_error() -> ProgrammingError:
    return ProgrammingError(
        "UPDATE auditcore.payments ...", {},
        Exception("Only a Delivery-stage payment may reference a Delivery"),
    )


def test_three_failures_in_a_row_give_up(journey, failing_sync):
    page = _page(journey)
    failing_sync["exc"] = _db_rule_error()
    assert _copy(journey, page) is None
    assert _copy(journey, page) is None
    assert _copy(journey, page) == worker.FACTS_COPY_GAVE_UP
    assert _attempts(journey, page["queue_id"]) == 3


def test_a_transient_failure_never_counts(journey, failing_sync):
    page = _page(journey)
    failing_sync["exc"] = OperationalError("SELECT 1", {}, Exception("canceling statement due to statement timeout"))
    for _ in range(5):
        assert _copy(journey, page) is None
    assert _attempts(journey, page["queue_id"]) == 0


def test_given_up_page_settles_as_failed_with_a_plain_reason():
    outcome = worker.classify_page_outcome(
        di_item={"state": "CLASSIFIED", "processingStatus": "PROCESSED"},
        extracted_count=0,
        submitted_at=datetime.now(UTC),
        facts_copy=worker.FACTS_COPY_GAVE_UP,
        now=datetime.now(UTC),
    )
    assert outcome.status == "FAILED"
    assert outcome.status in worker._PAGE_SETTLED_STATES
    assert "not a problem with the scan" in (outcome.reason or "")


def test_a_failure_that_has_not_given_up_still_waits():
    outcome = worker.classify_page_outcome(
        di_item={"state": "CLASSIFIED", "processingStatus": "PROCESSED"},
        extracted_count=0,
        submitted_at=datetime.now(UTC),
        facts_copy=None,
        now=datetime.now(UTC),
    )
    assert outcome.status == "SYNCING_TO_AUDIT_CORE"
    assert outcome.status not in worker._PAGE_SETTLED_STATES
