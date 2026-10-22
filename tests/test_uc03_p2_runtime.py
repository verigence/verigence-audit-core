"""Phase 2 runtime reliability: queue leases, retries, DI reconciliation,
fact sweep, field corrections and task state guards (real Postgres)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    AllowAllAuthorization,
    add_extracted_field,
    add_page,
    create_p2_journey,
    database_engine,
    principal,
    queue_row,
)
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_documents import (
    P2FieldCorrectionCommand,
    correct_p2_document_field,
)
from audit_core.uc03_p2_task_producer import sync_processing_failure_tasks
from audit_core.uc03_p2_tasks import TaskStateError, create_p2_task, submit_action


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine)
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _enqueue(journey, work_type="STAGE_RECOMPUTE", key=None, **kwargs):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker._enqueue(
            connection,
            tenant_id=journey.tenant_id,
            journey_id=journey.journey_id,
            work_type=work_type,
            work_key=key or f"test:{uuid4().hex}",
            payload={},
            correlation_id=None,
            **kwargs,
        )


def _expire_lease(journey, work_id):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_work_queue SET lease_expires_at_utc=now() - interval '1 second' "
                 "WHERE tenant_id=:t AND work_id=:w"),
            {"t": journey.tenant_id, "w": work_id},
        )


# --------------------------------------------------------------- page outcomes

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("di_item", "fields", "submitted_ago", "facts_copy", "expected"),
    [
        (None, 3, 1, None, "READY"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSING"}, 0, 1, None, "EXTRACTING"),
        ({"state": "CLASSIFYING"}, 0, 1, None, "CLASSIFYING"),
        ({"state": "UNKNOWN"}, 0, 1, None, "SUPPORTING"),
        ({"state": "FAILED"}, 0, 1, None, "FAILED"),
        ({"state": "CLASSIFIED", "processingStatus": "FAILED"}, 0, 1, None, "FAILED"),
        ({"state": "DELETED"}, 0, 1, None, "CANCELLED"),
        # DI is done; the worker's own copy decides, never a timer.
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSED"}, 0, 2, None, "SYNCING_TO_AUDIT_CORE"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSED"}, 0, 20, None, "SYNCING_TO_AUDIT_CORE"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSED"}, 0, 2, "COPIED", "NEEDS_REVIEW"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSED"}, 0, 2, "UNLINKED", "SUPPORTING"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSED"}, 3, 2, "COPIED", "READY"),
        # Load is never a failure: a page DI still has in hand is waited for.
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSED"}, 0, 61, None, "SYNCING_TO_AUDIT_CORE"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSING"}, 0, 45, None, "EXTRACTING"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSING"}, 0, 601, None, "EXTRACTING"),
        # Queued and waiting its turn: still in hand, however long.
        ({"state": "CLASSIFIED", "processingStatus": "PENDING", "extractionQueued": True}, 0, 601, None, "EXTRACTING"),
        ({"state": "CLASSIFIED", "processingStatus": "PENDING"}, 0, 601, None, "EXTRACTING"),
        # Classified and never queued (extra copy, no slot, no profile): settled at once.
        ({"state": "CLASSIFIED", "processingStatus": "PENDING", "extractionQueued": False}, 0, 1, None, "SUPPORTING"),
        ({"state": "STORED"}, 0, 601, None, "CLASSIFYING"),
        (None, 0, 1, None, "CLASSIFYING"),
        (None, 0, 10, None, "FAILED"),
    ],
)
def test_page_outcome_follows_di_never_a_timer(di_item, fields, submitted_ago, facts_copy, expected):
    outcome = worker.classify_page_outcome(
        di_item=di_item,
        extracted_count=fields,
        submitted_at=NOW - timedelta(minutes=submitted_ago),
        facts_copy=facts_copy,
        now=NOW,
    )
    assert outcome.status == expected
    if expected in {"FAILED", "NEEDS_REVIEW", "CANCELLED", "SUPPORTING"}:
        assert outcome.reason


_AI = {"failureKind": "AI", "scanPassed": True}
_AI_RETRYING = "AI reading failed — it is tried again 10 and 20 minutes after the first try. Nothing to do now."
_THREE = "tried 3 times (at once, then 10 and 20 minutes later)"
_NIGHT_DUE = "2026-10-21T17:00:00+00:00"


@pytest.mark.parametrize(
    ("di_item", "status", "reason", "last_error"),
    [
        # Retries DI has scheduled (T0+10, T0+20): the page stays in hand and says which case it is.
        ({"state": "CLASSIFIED", "processingStatus": "RETRY_PENDING", "processingFailureCode": "DOCUMENT_AI_UNAVAILABLE",
          **_AI}, "EXTRACTING", _AI_RETRYING, None),
        ({"state": "STORED", **_AI}, "CLASSIFYING", _AI_RETRYING, None),
        ({"state": "STORED", "failureKind": "TECHNICAL", "scanPassed": None}, "CLASSIFYING",
         ("Document service had a problem with this page — it is tried again 10 and 20 minutes after the first "
          "try. Nothing to do yet."), None),
        # The night re-read of a page our setup refused, queued or under way: in hand, never "failed".
        ({"state": "CLASSIFIED", "processingStatus": "FAILED", "processingFailureCode": "DOCUMENT_AI_NOT_CONFIGURED",
          "nextAttemptAtUtc": _NIGHT_DUE, "inNightAttempt": True, "retryTonight": True,
          "failureKind": "CONFIGURATION", "scanPassed": True}, "EXTRACTING",
         "Our setup problem — being read again now that it is fixed. Nothing to do now.", None),
        # The attempt at T0+20 failed too: FAILED, the PC resubmits (2026-10-21: no night attempt).
        ({"state": "CLASSIFIED", "processingStatus": "FAILED", "processingFailureCode": "DOCUMENT_AI_RESPONSE_INVALID",
          "attemptsMade": 3, **_AI}, "FAILED", f"AI reading failed — {_THREE}. Please upload this page again.",
         "DOCUMENT_AI_RESPONSE_INVALID"),
        ({"state": "FAILED", "failureCode": "DOCUMENT_AI_RATE_LIMITED", **_AI}, "FAILED",
         "AI reading failed — tried once. Please upload this page again.", "DOCUMENT_AI_RATE_LIMITED"),
        # The AI refused outright: at once.
        ({"state": "CLASSIFIED", "processingStatus": "FAILED", "processingFailureCode": "DOCUMENT_AI_REQUEST_REJECTED",
          **_AI}, "FAILED", "AI couldn't read this page — please upload it again.", "DOCUMENT_AI_REQUEST_REJECTED"),
        # An AI failure on a page that never passed the scan: reload it.
        ({"state": "FAILED", "failureCode": "DOCUMENT_AI_UNAVAILABLE", "failureKind": "AI", "scanPassed": None},
         "FAILED", "File couldn't be scanned — please reload it.", "DOCUMENT_AI_UNAVAILABLE"),
        # A technical failure inside DI after its three attempts.
        ({"state": "FAILED", "failureCode": "CLASSIFICATION_FAILED", "failureKind": "TECHNICAL", "scanPassed": True,
          "attemptsMade": 3}, "FAILED",
         f"Document service couldn't process this page — {_THREE}. Retry the page from the card, or upload it again.",
         "CLASSIFICATION_FAILED"),
        # A refusal of the content: at once, in words.
        ({"state": "CLASSIFIED", "processingStatus": "FAILED", "processingFailureCode": "DOCUMENT_AI_CONTENT_BLOCKED",
          "failureKind": "DOCUMENT", "scanPassed": True}, "FAILED",
         "AI declined to read this page's content — check the page and upload it again.",
         "DOCUMENT_AI_CONTENT_BLOCKED"),
        # Gemini refused our key or model: ours to fix, never "upload it again".
        ({"state": "CLASSIFIED", "processingStatus": "FAILED", "processingFailureCode": "DOCUMENT_AI_NOT_CONFIGURED",
          "failureKind": "CONFIGURATION", "scanPassed": True, "retryTonight": True}, "FAILED",
         "Our setup problem — will be re-read once fixed; no need to re-upload.", "DOCUMENT_AI_NOT_CONFIGURED"),
        ({"state": "FAILED", "failureCode": "DOCUMENT_TYPE_NOT_FOUND", "failureKind": "CONFIGURATION",
          "scanPassed": True, "retryTonight": False}, "FAILED",
         ("Document Intelligence is not set up to read this page on our side. Ask your Team Lead to raise it "
          "with support, then retry the page from the card once it is fixed."), "DOCUMENT_TYPE_NOT_FOUND"),
    ],
)
def test_page_outcome_says_which_retry_case_the_page_is_in(di_item, status, reason, last_error):
    """Product owner's rule (2026-10-21)."""
    outcome = worker.classify_page_outcome(
        di_item=di_item, extracted_count=0, submitted_at=NOW - timedelta(minutes=5), facts_copy=None, now=NOW,
    )
    assert outcome.status == status
    if reason is not None:
        assert outcome.reason == reason
    assert outcome.last_error == last_error


# ----------------------------------------------------------------- queue lease

def test_request_during_processing_is_not_lost(journey):
    _enqueue(journey, key="k1")
    [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    _enqueue(journey, key="k1")  # new request arrives while the item runs
    worker._complete(journey.engine, item)
    assert queue_row(journey, "STAGE_RECOMPUTE", "k1")["work_status"] == "PENDING"

    [again] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    worker._complete(journey.engine, again)
    assert queue_row(journey, "STAGE_RECOMPUTE", "k1")["work_status"] == "COMPLETED"


def test_expired_lease_cannot_overwrite_reclaimed_work(journey):
    _enqueue(journey, key="k2")
    [first] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    _expire_lease(journey, first.work_id)
    [second] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    assert second.lease_token != first.lease_token
    assert second.attempt_count == first.attempt_count + 1  # a dead worker is a failed attempt

    with pytest.raises(worker.LeaseLost):
        worker._complete(journey.engine, first)
    assert queue_row(journey, "STAGE_RECOMPUTE", "k2")["work_status"] == "CLAIMED"
    worker._complete(journey.engine, second)
    assert queue_row(journey, "STAGE_RECOMPUTE", "k2")["work_status"] == "COMPLETED"


def test_crash_looping_item_dead_letters(journey, monkeypatch):
    monkeypatch.setattr(worker, "_MAX_ATTEMPTS", 3)
    _enqueue(journey, key="k3")
    for _ in range(3):
        items = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
        if not items:
            break
        _expire_lease(journey, items[0].work_id)
    worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    assert queue_row(journey, "STAGE_RECOMPUTE", "k3")["work_status"] == "DEAD_LETTER"


def test_failures_back_off_then_dead_letter_and_fail_the_page(journey, monkeypatch):
    """Product owner's rule (2026-10-21): a technical failure sending a page
    to the document service is tried at T0, T0+10 and T0+20 minutes, then
    the page fails and the PC is asked to resubmit it."""
    from audit_core.di_capture_v2_client import DiCaptureV2Error

    down = DiCaptureV2Error(status_code=503, detail="down")
    batch_id, queue_id, _ = add_page(journey, status="DI_UPLOAD_PREPARING")
    _enqueue(journey, work_type="DOCUMENT_INGEST", key=str(queue_id))

    [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    worker._fail(journey.engine, item, down)
    row = queue_row(journey, "DOCUMENT_INGEST", str(queue_id))
    assert row["work_status"] == "RETRY_WAIT" and row["attempt_count"] == 1
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        wait = connection.execute(
            text("SELECT EXTRACT(EPOCH FROM next_attempt_at_utc - updated_at_utc) FROM auditcore.p2_work_queue "
                 "WHERE work_id=:w"),
            {"w": row["work_id"]},
        ).scalar_one()
    assert 595 <= float(wait) <= 605
    # The PC sees the page waiting for its retry, and why.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        waiting = connection.execute(
            text("SELECT queue_status, status_reason FROM auditcore.p2_document_queue WHERE queue_id=:q"),
            {"q": queue_id},
        ).mappings().one()
    assert waiting["queue_status"] == "RETRY_WAIT"
    # DI answered with an error (503): it did respond (2026-10-20 review wording)
    assert waiting["status_reason"] == ("Document service returned an error — trying again in 10 minutes "
                                        "(attempt 1 of 3). Nothing to do yet.")
    # A transient failure is not worth a task: the file is minutes old.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert connection.execute(
            text("SELECT COUNT(*) FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one() == 0
        # Safety net only (the 30-minute ceiling fails such a page first): an
        # hour on, still retrying, one Info task for the file.
        connection.execute(
            text("UPDATE auditcore.p2_upload_batches SET created_at_utc=now() - interval '61 minutes' "
                 "WHERE tenant_id=:t AND batch_id=:b"),
            {"t": journey.tenant_id, "b": batch_id},
        )
        assert sync_processing_failure_tasks(connection, tenant_id=journey.tenant_id,
                                             journey_id=journey.journey_id) == {"RAISED": 1}
        task = connection.execute(
            text("SELECT task_type, severity, priority, title, description, task_status, reference "
                 "FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k"),
            {"t": journey.tenant_id, "k": f"upload-status:{journey.journey_id}:{batch_id}"},
        ).mappings().one()
    assert task["task_type"] == "PC_UPLOAD_STATUS" and task["severity"] == "INFO" and task["priority"] == "LOW"
    assert task["task_status"] == "READY"
    assert task["title"] == "scan.pdf: 0 of 1 pages read, still working"
    assert "1 waiting for an automatic retry" in task["description"]
    assert task["reference"]["retrying"] is True and task["reference"]["pageNumbers"] == []

    for _ in range(2):  # T0+10 and T0+20 fail too
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            connection.execute(
                text("UPDATE auditcore.p2_work_queue SET next_attempt_at_utc=now() WHERE work_id=:w"),
                {"w": row["work_id"]},
            )
        [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
        worker._fail(journey.engine, item, down)
    assert queue_row(journey, "DOCUMENT_INGEST", str(queue_id))["work_status"] == "DEAD_LETTER"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        page = connection.execute(
            text("SELECT queue_status, status_reason FROM auditcore.p2_document_queue WHERE queue_id=:q"),
            {"q": queue_id},
        ).mappings().one()
    assert page["queue_status"] == "FAILED"
    assert page["status_reason"] == ("Document service returned an error — tried 3 times (at once, then 10 and 20 "
                                     "minutes later). Retry the page from the card, or upload it again.")
    # ...and the file's status task is replaced by its "Document to resubmit" task.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        tasks = connection.execute(
            text("SELECT task_type, category, assigned_role_code, severity, title, description, reference "
                 "FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j AND task_status='READY'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().all()
    [task] = tasks
    assert task["task_type"] == "DOCUMENT_RESUBMIT" and task["category"] == "DOCUMENT_RESUBMIT"
    assert task["assigned_role_code"] == "PC" and task["severity"] == "HIGH"
    assert task["title"] == "scan.pdf: 1 page to resubmit"
    assert task["description"].startswith("Page 1: Document service returned an error — tried 3 times")
    assert task["reference"]["pageNumbers"] == [1]


def test_retries_are_never_within_five_minutes():
    """Background steps (reconcile, recompute, controls) keep the spread-out
    ladder of 2026-10-01."""
    delays = [worker._retry_delay("STAGE_RECOMPUTE", n) for n in (1, 2, 3, 4)]
    assert delays == [900, 1800, 3600, 7200]  # decision 2026-10-01: spread out, never within five minutes
    assert min(delays) >= 300
    assert worker._max_attempts("STAGE_RECOMPUTE") == 5


@pytest.mark.parametrize(
    ("work_type", "payload"),
    [("JOURNEY_RECONCILE", None), ("DOCUMENT_RECONCILE", None), ("DOCUMENT_INGEST", {"recover": "n"})],
)
def test_polling_di_never_stretches_to_hours(work_type, payload):
    """2026-10-20 review (S1, S2): checking DI for results, and the nightly
    look-up of a page our setup refused, retry after 1, 2, then
    every 5 minutes, and stop (telling the Team Lead) after about an hour."""
    delays = [worker._retry_delay(work_type, n, payload) for n in range(1, 13)]
    assert delays[:3] == [60, 120, 300] and max(delays) == 300
    assert worker._max_attempts(work_type, payload) == 13
    assert sum(delays) < 3600


def test_the_pcs_retry_lookup_keeps_the_three_attempt_rule():
    assert worker._max_attempts("DOCUMENT_INGEST", {"recover": "r"}) == 3


@pytest.mark.parametrize("work_type", ["DOCUMENT_INGEST", "SPLIT_BATCH", "PAGE_REREAD", "BATCH_GROUP"])
def test_a_documents_own_steps_get_three_attempts_ten_minutes_apart(work_type):
    """Product owner's rule (2026-10-21): T0, T0+10, T0+20 minutes."""
    assert worker._max_attempts(work_type) == 3
    assert [worker._retry_delay(work_type, n) for n in (1, 2)] == [600, 600]


def test_a_request_the_document_service_refuses_fails_at_once(journey, monkeypatch):
    """No retry for an error that cannot recover: the page fails on the first
    attempt with the reason, and the PC's task is raised."""
    from audit_core.di_capture_v2_client import DiCaptureV2Error

    _, queue_id, _ = add_page(journey, status="DI_UPLOAD_PREPARING")
    _enqueue(journey, work_type="DOCUMENT_INGEST", key=str(queue_id))
    [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    refused = DiCaptureV2Error(status_code=400, detail=(
        '{"code":"INVALID_REQUEST","title":"Request syntax/parameters/body are invalid.","status":400,'
        '"retryable":false,"detail":"Request validation failed for body.files.0.sizeBytes."}'))
    assert worker._is_retryable(refused) is False
    assert worker._is_retryable(DiCaptureV2Error(status_code=503, detail="down")) is True
    assert worker._is_retryable(ValueError("Encrypted PDF is not supported")) is False
    worker._fail(journey.engine, item, refused)
    assert queue_row(journey, "DOCUMENT_INGEST", str(queue_id))["work_status"] == "DEAD_LETTER"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        page = connection.execute(
            text("SELECT queue_status, status_reason FROM auditcore.p2_document_queue WHERE queue_id=:q"),
            {"q": queue_id},
        ).mappings().one()
        tasks = connection.execute(
            text("SELECT COUNT(*) FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                 "AND task_type='DOCUMENT_RESUBMIT' AND severity='HIGH' AND task_status='READY'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()
    assert page["queue_status"] == "FAILED"
    assert page["status_reason"].startswith("The document service could not accept this page: Request validation failed")
    assert "Not processed after" not in page["status_reason"]
    assert tasks == 1


def test_per_journey_concurrency_cap(journey, monkeypatch):
    monkeypatch.setattr(worker, "_PER_JOURNEY_CONCURRENCY", 2)
    for index in range(5):
        _enqueue(journey, key=f"cap-{index}")
    first = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    assert len(first) == 2
    assert worker._claim_for_tenant(journey.engine, journey.tenant_id, 10) == []
    worker._complete(journey.engine, first[0])
    assert len(worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)) == 1


# ------------------------------------------------------- Journey reconciliation

class FakeCaptureClient:
    def __init__(self, documents):
        self.documents = documents
        self.list_calls = 0

    def list_documents(self, *, token, tenant_id, external_context_ref, phase):
        self.list_calls += 1
        return {"documents": self.documents if phase == "BOOKING" else []}


def _reconcile(journey, monkeypatch, documents, copy=None):
    """``copy`` stands in for _copy_page_facts (DI and Security are not
    reachable here): None means "could not copy this round"."""
    client = FakeCaptureClient(documents)
    monkeypatch.setattr(worker, "_di_context_and_requirements", lambda engine, work: ("ctx", "tok", [], {}))
    monkeypatch.setattr(worker, "get_di_capture_v2_client", lambda: client)
    monkeypatch.setattr(worker, "_copy_page_facts", copy or (lambda engine, **kwargs: None))
    _enqueue(journey, work_type="JOURNEY_RECONCILE", key=str(journey.journey_id))
    [item] = [
        i for i in worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
        if i.work_type == "JOURNEY_RECONCILE"
    ]
    try:
        worker._journey_reconcile(journey.engine, item)
    except worker.RescheduleWork:
        worker._reschedule(journey.engine, item, worker.RescheduleWork("wait", delay_seconds=2))
        return client, "RESCHEDULED"
    worker._complete(journey.engine, item)
    return client, "DONE"


def _page_status(journey, queue_id):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT queue_status FROM auditcore.p2_document_queue WHERE queue_id=:q"),
            {"q": queue_id},
        ).scalar_one()


def test_one_di_listing_settles_every_page_of_the_journey(journey, monkeypatch):
    _, unknown_q, unknown_d = add_page(journey, page_number=1)
    _, failed_q, failed_d = add_page(journey, page_number=2)
    _, ready_q, ready_d = add_page(journey, page_number=3)
    add_extracted_field(journey, di_document_id=ready_d, field_key="customer_name", value="A")

    client, outcome = _reconcile(journey, monkeypatch, [
        {"documentId": str(unknown_d), "state": "UNKNOWN"},
        {"documentId": str(failed_d), "state": "FAILED"},
        {"documentId": str(ready_d), "state": "CLASSIFIED", "processingStatus": "PROCESSED",
         "classifiedDocumentTypeKey": "booking_form"},
    ])
    assert outcome == "DONE"
    assert client.list_calls == 2  # one listing per DI phase, not per page
    assert _page_status(journey, unknown_q) == "SUPPORTING"
    assert _page_status(journey, failed_q) == "FAILED"
    assert _page_status(journey, ready_q) == "READY"
    assert queue_row(journey, "STAGE_RECOMPUTE", f"booking:{journey.journey_id}") is not None
    # Issue 9 (2026-09-30): the page DI could not classify is a High task
    # for the PC straight away, not at the next stage recompute.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        tasks = connection.execute(
            text("SELECT task_type, severity, task_status FROM auditcore.p2_tasks "
                 "WHERE tenant_id=:t AND dedupe_key=:k"),
            {"t": journey.tenant_id, "k": f"unclassified:{journey.journey_id}:{unknown_q}"},
        ).mappings().all()
    assert [dict(t) for t in tasks] == [
        {"task_type": "PC_VERIFY_UNRECOGNIZED_DOCUMENT", "severity": "HIGH", "task_status": "READY"}]
    # 2026-09-30: the page DI rejected is named in the file's High task in
    # the same round, not left to a later stage recompute (2026-10-21: the
    # file's "Document to resubmit" task).
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        status = connection.execute(
            text("SELECT severity, task_status, title, reference FROM auditcore.p2_tasks "
                 "WHERE tenant_id=:t AND journey_id=:j AND task_type='DOCUMENT_RESUBMIT'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().all()
    assert len(status) == 1 and status[0]["severity"] == "HIGH" and status[0]["task_status"] == "READY"
    assert status[0]["title"] == "scan.pdf: 1 page to resubmit" and status[0]["reference"]["pageNumbers"] == [2]


def test_a_page_marked_nothing_read_turns_ready_once_its_facts_arrive(journey, monkeypatch):
    """Issue 5 (2026-09-30): the fact sync ran late, so the page was marked
    NEEDS_REVIEW ("no fields could be extracted"). The facts then arrived.
    The next reconcile (an upload, Recheck documents) must turn it READY
    instead of leaving the card on "check" for good."""
    _, queue_id, di_document_id = add_page(journey, status="NEEDS_REVIEW", submitted_minutes_ago=20)
    add_extracted_field(journey, di_document_id=di_document_id, field_key="customer_name", value="A")
    _, outcome = _reconcile(journey, monkeypatch, [
        {"documentId": str(di_document_id), "state": "CLASSIFIED", "processingStatus": "PROCESSED",
         "classifiedDocumentTypeKey": "booking_form"},
    ])
    assert outcome == "DONE"
    assert _page_status(journey, queue_id) == "READY"
    assert queue_row(journey, "STAGE_RECOMPUTE", f"booking:{journey.journey_id}") is not None


def test_in_flight_pages_reschedule_instead_of_failing(journey, monkeypatch):
    _, queue_id, di_document_id = add_page(journey)
    _, outcome = _reconcile(journey, monkeypatch, [
        {"documentId": str(di_document_id), "state": "CLASSIFIED", "processingStatus": "PROCESSING"},
    ])
    assert outcome == "RESCHEDULED"
    assert _page_status(journey, queue_id) == "EXTRACTING"
    row = queue_row(journey, "JOURNEY_RECONCILE", str(journey.journey_id))
    assert row["work_status"] == "PENDING" and row["attempt_count"] == 0


# ------------------------------------------------------------------ fact sweep

def test_fact_sweep_detects_external_fact_changes_once(journey):
    di_document_id = uuid4()
    add_extracted_field(journey, di_document_id=di_document_id, field_key="pan_number", value="X")
    assert worker._fact_sweep(journey.engine, journey.tenant_id) == 1
    assert worker._fact_sweep(journey.engine, journey.tenant_id) == 0
    add_extracted_field(journey, di_document_id=di_document_id, field_key="dob", value="1990-01-01")
    assert worker._fact_sweep(journey.engine, journey.tenant_id) == 1


# ------------------------------------------------------------ field correction

def test_repeated_corrections_never_lose_the_machine_value(journey):
    di_document_id = uuid4()
    add_page(journey, di_document_id=di_document_id)
    canonical = add_extracted_field(
        journey, di_document_id=di_document_id, field_key="p2_test_note", value="MACHINE", confidence=40.0,
    )
    for new_value in ("FIRST", "SECOND"):
        with journey.engine.begin() as connection:
            correct_p2_document_field(
                tenant_id=journey.tenant_id,
                journey_id=journey.journey_id,
                document_id=di_document_id,
                command=P2FieldCorrectionCommand(
                    canonicalFieldId=canonical, fieldKey="p2_test_note",
                    sourceFactVersion=1, newValue=new_value,
                ),
                human_principal=principal(journey),
                authorization_client=AllowAllAuthorization(),
                connection=connection,
            )
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        row = connection.execute(
            text("SELECT extracted_value, effective_value, is_modified "
                 "FROM auditcore.journey_document_extracted_fields "
                 "WHERE tenant_id=:t AND di_document_id=:d AND field_key='p2_test_note'"),
            {"t": journey.tenant_id, "d": di_document_id},
        ).mappings().one()
    assert row["extracted_value"] == "MACHINE"
    assert row["effective_value"] == "SECOND"
    assert row["is_modified"] is True


def test_high_confidence_correction_approval_keeps_machine_value_and_detects_staleness(journey):
    di_document_id = uuid4()
    add_page(journey, di_document_id=di_document_id)
    canonical = add_extracted_field(
        journey, di_document_id=di_document_id, field_key="p2_test_amount", value="100", confidence=98.0,
    )
    with journey.engine.begin() as connection:
        proposal = correct_p2_document_field(
            tenant_id=journey.tenant_id,
            journey_id=journey.journey_id,
            document_id=di_document_id,
            command=P2FieldCorrectionCommand(
                canonicalFieldId=canonical, fieldKey="p2_test_amount",
                sourceFactVersion=1, newValue="150", remarks="Receipt shows 150",
            ),
            human_principal=principal(journey),
            authorization_client=AllowAllAuthorization(),
            connection=connection,
        )
    assert proposal["applied"] is False
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        result = submit_action(
            connection, tenant_id=journey.tenant_id, task_id=proposal["taskId"],
            action="APPROVE_CORRECTION", actor_id="tl-1", actor_role_code="TL", comment=None,
        )
    assert result["status"] == "VERIFIED_COMPLETE"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        row = connection.execute(
            text("SELECT extracted_value, effective_value FROM auditcore.journey_document_extracted_fields "
                 "WHERE tenant_id=:t AND di_document_id=:d AND field_key='p2_test_amount'"),
            {"t": journey.tenant_id, "d": di_document_id},
        ).mappings().one()
    assert (row["extracted_value"], row["effective_value"]) == ("100", "150")

    # A proposal made against 150 goes stale when the value changes first.
    with journey.engine.begin() as connection:
        stale = correct_p2_document_field(
            tenant_id=journey.tenant_id, journey_id=journey.journey_id, document_id=di_document_id,
            command=P2FieldCorrectionCommand(
                canonicalFieldId=canonical, fieldKey="p2_test_amount",
                sourceFactVersion=1, newValue="175", remarks="typo",
            ),
            human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
            connection=connection,
        )
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.journey_document_extracted_fields SET effective_value=to_jsonb('160'::text) "
                 "WHERE tenant_id=:t AND di_document_id=:d AND field_key='p2_test_amount'"),
            {"t": journey.tenant_id, "d": di_document_id},
        )
    with journey.engine.begin() as connection, pytest.raises(TaskStateError):
        set_tenant_context(connection, journey.tenant_id)
        submit_action(connection, tenant_id=journey.tenant_id, task_id=stale["taskId"],
                      action="APPROVE_CORRECTION", actor_id="tl-1", actor_role_code="TL", comment=None)


# ---------------------------------------------------------------- task guards

def _machine_task(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return create_p2_task(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            task_type="WRONG_DOCUMENT_REVIEW", category="DOCUMENT_VERIFICATION",
            origin_kind="SYSTEM", source_type="RULE", source_code="WRONG_DOCUMENT",
            dedupe_key=f"test:{uuid4()}", title="Review", description="Review it",
            reference={}, severity="HIGH", priority="HIGH", assigned_role_code="PC",
            assigned_actor_id=None, raised_by_actor_id=None, raised_by_role_code=None,
            allowed_actions=["REVIEW_DOCUMENT", "ADD_COMMENT"], completion_protocol="MACHINE_VERIFIED",
        )


def test_submitted_task_cannot_be_actioned_again_but_accepts_comments(journey):
    task_id = _machine_task(journey)

    def act(action, comment=None):
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            return submit_action(connection, tenant_id=journey.tenant_id, task_id=task_id,
                                 action=action, actor_id=journey.actor_id, actor_role_code="PC",
                                 comment=comment)

    assert act("REVIEW_DOCUMENT")["status"] == "VERIFYING"
    with pytest.raises(TaskStateError):
        act("REVIEW_DOCUMENT")
    assert act("ADD_COMMENT", "checked with dealer")["taskId"] == str(task_id)


def test_claim_time_dead_letter_fails_the_page_and_raises_the_pc_task(journey, monkeypatch):
    # a page's ingest gets three attempts (2026-10-21): the third expired lease dead-letters it
    batch_id, queue_id, _ = add_page(journey, status="DI_UPLOAD_PREPARING")
    _enqueue(journey, work_type="DOCUMENT_INGEST", key=str(queue_id))
    for _ in range(3):
        items = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
        if not items:
            break
        _expire_lease(journey, items[0].work_id)
    worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    assert queue_row(journey, "DOCUMENT_INGEST", str(queue_id))["work_status"] == "DEAD_LETTER"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        page = connection.execute(
            text("SELECT queue_status, status_reason FROM auditcore.p2_document_queue WHERE queue_id=:q"),
            {"q": queue_id},
        ).mappings().one()
        task = connection.execute(
            text("SELECT task_status FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k"),
            {"t": journey.tenant_id, "k": f"resubmit:{journey.journey_id}:{batch_id}"},
        ).mappings().one_or_none()
    # Before: the page stayed "in progress" forever with no task.
    assert page["queue_status"] == "FAILED" and "interrupted repeatedly" in page["status_reason"]
    assert task is not None and task["task_status"] == "READY"


def test_stored_and_shown_errors_never_carry_raw_exception_text(journey, monkeypatch):
    monkeypatch.setattr(worker, "_DOCUMENT_MAX_ATTEMPTS", 1)
    _, queue_id, _ = add_page(journey, status="DI_UPLOAD_PREPARING")
    _enqueue(journey, work_type="DOCUMENT_INGEST", key=str(queue_id))
    [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    raw = "PUT https://bucket/k?X-Amz-Signature=deadbeef for customer Ravi Kumar"
    try:
        raise RuntimeError(raw)
    except RuntimeError as exc:
        worker._fail(journey.engine, item, exc)
    row = queue_row(journey, "DOCUMENT_INGEST", str(queue_id))
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        page = connection.execute(
            text("SELECT status_reason FROM auditcore.p2_document_queue WHERE queue_id=:q"), {"q": queue_id},
        ).mappings().one()
        activity = connection.execute(
            text("SELECT details::text AS d FROM auditcore.p2_activity_events WHERE tenant_id=:t "
                 "AND event_type='P2_WORK_DEAD_LETTER'"),
            {"t": journey.tenant_id},
        ).mappings().all()
    stored = " ".join([row["last_error"], page["status_reason"], *(a["d"] for a in activity)])
    assert "deadbeef" not in stored and "Ravi" not in stored
    assert row["last_error"].startswith("RuntimeError (at ") and "test_uc03_p2_runtime.py" in row["last_error"]
    assert "system problem" in page["status_reason"]


def test_running_items_keep_their_lease_until_they_settle(journey, monkeypatch):
    import threading
    import time as time_module

    monkeypatch.setattr(worker, "_LEASE_RENEW_SECONDS", 0.2)
    _enqueue(journey, key="long-running")
    [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    _expire_lease(journey, item.work_id)  # as if the item had been running for the whole lease
    worker._started_at[item.work_id] = time_module.perf_counter()
    done = threading.Event()
    renewer = threading.Thread(target=worker._keep_leases, args=(journey.engine, [item], done))
    renewer.start()
    time_module.sleep(0.6)
    done.set()
    renewer.join()
    worker._started_at.pop(item.work_id, None)
    # Renewed: another worker cannot reclaim it while it is still running.
    assert worker._claim_for_tenant(journey.engine, journey.tenant_id, 10) == []


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (RuntimeError("connection reset"), True),
        (ValueError("Encrypted PDF is not supported"), False),
        (KeyError("pages"), False),
        (TypeError("unsupported operand"), False),
    ],
)
def test_programming_errors_are_not_retried(exc, retryable):
    assert worker._is_retryable(exc) is retryable


# ------------------------------------------------------------ nightly upload sweep

def _set_page_age(journey, queue_id, *, hours: int) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET updated_at_utc=now() - (:h * interval '1 hour') "
                 "WHERE tenant_id=:t AND queue_id=:q"),
            {"t": journey.tenant_id, "q": queue_id, "h": hours},
        )


def _upload_id(journey, queue_id) -> str:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT client_upload_id FROM auditcore.p2_document_queue WHERE queue_id=:q"), {"q": queue_id},
        ).scalar_one()


def _recover(journey, monkeypatch, documents):
    """Run every DOCUMENT_INGEST work item due (the recovery requests),
    against a DI listing of ``documents``."""
    client = FakeCaptureClient(documents)
    monkeypatch.setattr(worker, "_di_context_and_requirements", lambda engine, work: ("ctx", "tok", [], {}))
    monkeypatch.setattr(worker, "get_di_capture_v2_client", lambda: client)
    items = [i for i in worker._claim_for_tenant(journey.engine, journey.tenant_id, 10) if i.work_type == "DOCUMENT_INGEST"]
    assert items, "no recovery work was queued"
    for item in items:
        worker._ingest_document(journey.engine, item)
        worker._complete(journey.engine, item)
    return client


def _set_last_error(journey, queue_id, value):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET last_error=:e WHERE queue_id=:q"),
            {"q": queue_id, "e": value},
        )


_SETUP_FAILED = {"state": "CLASSIFIED", "processingStatus": "FAILED",
                 "processingFailureCode": "DOCUMENT_AI_NOT_CONFIGURED", "failureKind": "CONFIGURATION",
                 "scanPassed": True}


def test_nightly_sweep_looks_again_only_at_pages_our_setup_refused(journey, monkeypatch):
    """Product owner's rule (2026-10-21): the sweep never sends a failed page
    again. Only a page our setup refused (DI re-reads it once that night) is
    looked up at DI; an AI or technical failure and a scan-quality
    rejection stay with the PC (their resubmit task); a page still in
    flight after a day is reconciled (a status read, not an attempt)."""
    _, setup, _ = add_page(journey, page_number=1, status="FAILED")
    _, ai, _ = add_page(journey, page_number=2, status="FAILED")
    _, quality, _ = add_page(journey, page_number=3, status="FAILED")
    _, stuck, _ = add_page(journey, page_number=4, status="CLASSIFYING")
    _set_last_error(journey, setup, "DOCUMENT_AI_NOT_CONFIGURED")
    _set_last_error(journey, ai, "DOCUMENT_AI_UNAVAILABLE")
    _set_last_error(journey, quality, "DI_QUALITY_BLUR")
    _set_page_age(journey, stuck, hours=25)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        swept = worker.queue_nightly_upload_sweep(connection, tenant_id=journey.tenant_id)
    assert swept == {"retried": 1, "reconciled": 1, "reread": 0, "journeys": 1}
    assert _page_status(journey, setup) == "QUEUED"
    ingest = queue_row(journey, "DOCUMENT_INGEST", str(setup))
    assert ingest["work_status"] == "PENDING" and ingest["payload"]["recover"] == "n"
    assert _page_status(journey, ai) == "FAILED"
    assert _page_status(journey, quality) == "FAILED"
    assert queue_row(journey, "JOURNEY_RECONCILE", str(journey.journey_id))["work_status"] == "PENDING"


def _setup_page(journey):
    batch_id, queue_id, document_id = add_page(journey, page_number=1, status="FAILED")
    _set_last_error(journey, queue_id, "DOCUMENT_AI_NOT_CONFIGURED")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker.queue_nightly_upload_sweep(connection, tenant_id=journey.tenant_id)
    return batch_id, queue_id, document_id


def _page(journey, queue_id):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT queue_status, status_reason, last_error, client_upload_id "
                 "FROM auditcore.p2_document_queue WHERE queue_id=:q"),
            {"q": queue_id},
        ).mappings().one()


def _file_tasks(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT severity, title, description FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                 "AND task_type='DOCUMENT_RESUBMIT' AND task_status='READY'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().all()


_AI_FAILED = {"state": "CLASSIFIED", "processingStatus": "FAILED", "processingFailureCode": "DOCUMENT_AI_UNAVAILABLE",
              "failureKind": "AI", "scanPassed": True}


def test_a_setup_page_whose_night_re_read_is_still_to_come_is_left(journey, monkeypatch):
    _, queue_id, document_id = _setup_page(journey)
    _recover(journey, monkeypatch, documents=[{**_SETUP_FAILED, "documentId": str(document_id),
                                               "retryTonight": True}])
    page = _page(journey, queue_id)
    assert page["queue_status"] == "FAILED" and "~" not in page["client_upload_id"]
    assert page["last_error"] == "DOCUMENT_AI_NOT_CONFIGURED"
    assert page["status_reason"] == "Our setup problem — will be re-read once fixed; no need to re-upload."
    [task] = _file_tasks(journey)
    assert task["severity"] == "INFO" and "no need to re-upload" in task["title"]


def test_a_setup_page_whose_night_re_read_failed_stays_with_its_reason(journey, monkeypatch):
    _, queue_id, document_id = _setup_page(journey)
    _recover(journey, monkeypatch, documents=[{**_SETUP_FAILED, "documentId": str(document_id),
                                               "retryTonight": False}])
    page = _page(journey, queue_id)
    assert page["queue_status"] == "FAILED" and "~" not in page["client_upload_id"], "never sent again"
    assert page["last_error"] == "DOCUMENT_AI_NOT_CONFIGURED"


def test_a_page_the_night_re_read_read_is_copied(journey, monkeypatch):
    _, queue_id, document_id = _setup_page(journey)
    _recover(journey, monkeypatch, documents=[{"documentId": str(document_id), "state": "CLASSIFIED",
                                               "processingStatus": "PROCESSED"}])
    assert _page_status(journey, queue_id) == "SYNCING_TO_AUDIT_CORE"


# ------------------------------------------------- recovery looks at DI first

@pytest.mark.parametrize(
    ("di_item", "marker", "submitted_ago_hours", "expected"),
    [
        (None, "r", 1, "REUPLOAD"),
        # Review of #469: the nightly look-up never sends a page again; a page
        # DI no longer holds is the PC's to resubmit.
        (None, "n", 1, "GONE"),
        ({"state": "DELETED"}, "n", 1, "GONE"),
        ({"state": "FAILED"}, "n", 1, "TELL_PC"),
        ({"state": "DELETED"}, "r", 1, "REUPLOAD"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSED"}, "r", 1, "SYNCING_TO_AUDIT_CORE"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSING"}, "n", 1, "EXTRACTING"),
        ({"state": "CLASSIFIED", "processingStatus": "RETRY_PENDING"}, "r", 1, "EXTRACTING"),
        ({"state": "STORED"}, "r", 1, "CLASSIFYING"),
        ({"state": "CLASSIFYING"}, "n", 1, "CLASSIFYING"),
        # A failed reading (a DI without failureKind): the nightly look-up
        # settles it for the PC; the PC's Retry sends it again.
        ({"state": "CLASSIFIED", "processingStatus": "FAILED", "failureCode": "DOCUMENT_AI_UNAVAILABLE"}, "n", 30,
         "TELL_PC"),
        ({"state": "CLASSIFIED", "processingStatus": "FAILED", "failureCode": "DOCUMENT_AI_UNAVAILABLE"}, "r", 30,
         "REUPLOAD"),
        ({"state": "CLASSIFIED", "processingStatus": "FAILED", "failureCode": "CORRUPT"}, "n", 30, "TELL_PC"),
        # Product owner's rule (2026-10-21), with DI's failureKind: the night
        # look-up (pages our setup refused) never sends a page again; it
        # leaves one whose night re-read is still to come and settles any
        # other. The PC's Retry still sends a given-up page again.
        ({**_SETUP_FAILED, "retryTonight": True}, "n", 1, "LEAVE"),
        ({**_SETUP_FAILED, "retryTonight": True}, "n", 100, "LEAVE"),
        ({**_SETUP_FAILED, "retryTonight": False}, "n", 1, "TELL_PC"),
        ({**_SETUP_FAILED, "retryTonight": True, "scanPassed": None}, "n", 1, "TELL_PC"),
        ({**_AI_FAILED, "retryTonight": False}, "n", 1, "TELL_PC"),
        ({**_AI_FAILED, "failureKind": "TECHNICAL", "processingFailureCode": "STORAGE_READ_ERROR"}, "n", 1, "TELL_PC"),
        ({"state": "FAILED", "failureCode": "DOCUMENT_AI_NOT_CONFIGURED", "failureKind": "CONFIGURATION",
          "scanPassed": True, "retryTonight": True}, "n", 1, "LEAVE"),
        ({"state": "FAILED", "failureCode": "CLASSIFICATION_FAILED", "failureKind": "TECHNICAL"}, "n", 1, "TELL_PC"),
        ({**_AI_FAILED, "retryTonight": False}, "r", 1, "REUPLOAD"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSED", "failureKind": None}, "n", 1, "SYNCING_TO_AUDIT_CORE"),
    ],
)
def test_recovery_decision_reads_di_before_uploading_again(di_item, marker, submitted_ago_hours, expected):
    decision = worker.recovery_decision(
        di_item=di_item, marker=marker, submitted_at=NOW - timedelta(hours=submitted_ago_hours), now=NOW,
    )
    assert decision == expected


def test_recovery_copies_a_page_di_already_read_instead_of_uploading_it_again(journey, monkeypatch):
    """Never re-read what DI already read (2026-09-30): a FAILED page whose
    DI document is PROCESSED goes back for a copy, no upload; one DI still
    has in hand is waited for; one DI's nightly will retry is left alone."""
    _, read_q, read_d = add_page(journey, page_number=1, status="FAILED")
    _, busy_q, busy_d = add_page(journey, page_number=2, status="FAILED")
    _, waiting_q, waiting_d = add_page(journey, page_number=3, status="FAILED")
    listing = [
        {"documentId": str(read_d), "state": "CLASSIFIED", "processingStatus": "PROCESSED"},
        {"documentId": str(busy_d), "state": "CLASSIFIED", "processingStatus": "PROCESSING"},
        {"documentId": str(waiting_d), "state": "CLASSIFIED", "processingStatus": "FAILED",
         "failureCode": "DOCUMENT_AI_UNAVAILABLE"},
    ]
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        for queue_id, marker in ((read_q, "r"), (busy_q, "r"), (waiting_q, "n")):
            worker.request_page_recovery(
                connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, queue_id=queue_id,
                marker=marker, requested_by="pc", correlation_id=None,
            )
    for queue_id in (read_q, busy_q, waiting_q):
        assert _page_status(journey, queue_id) == "QUEUED"
    _recover(journey, monkeypatch, documents=listing)
    assert _page_status(journey, read_q) == "SYNCING_TO_AUDIT_CORE"
    assert _page_status(journey, busy_q) == "EXTRACTING"
    assert _page_status(journey, waiting_q) == "FAILED"
    for queue_id in (read_q, busy_q, waiting_q):
        assert not _upload_id(journey, queue_id).count("~"), "no page was uploaded again"
        assert queue_row(journey, "DOCUMENT_INGEST", str(queue_id))["work_status"] == "COMPLETED"
    assert queue_row(journey, "JOURNEY_RECONCILE", str(journey.journey_id))["work_status"] == "PENDING"


# ------------------------------------------------- the worker copies the values

def test_processed_page_is_ready_once_the_worker_copied_its_values(journey, monkeypatch):
    """Issue 15 (2026-09-30): the worker copies a processed page's values
    itself; no webhook, no background task, no timer. READY when values
    landed, "nothing read" only when the copy ran and found none, and
    "still saving" (re-polled) when the copy could not run this round."""
    _, ready_q, ready_d = add_page(journey, page_number=1)
    _, empty_q, empty_d = add_page(journey, page_number=2)
    _, busy_q, busy_d = add_page(journey, page_number=3)
    _, extra_q, extra_d = add_page(journey, page_number=4)
    processed = {"state": "CLASSIFIED", "processingStatus": "PROCESSED", "classifiedDocumentTypeKey": "booking_form"}

    def copy(engine, *, work, page, di_item, requirement_refs):
        document_id = str(page["di_document_id"])
        if document_id == str(ready_d):
            add_extracted_field(journey, di_document_id=ready_d, field_key="customer_name", value="A")
            return "COPIED"
        if document_id == str(empty_d):
            return "COPIED"
        if document_id == str(extra_d):
            return "UNLINKED"
        return None

    _, outcome = _reconcile(journey, monkeypatch, [
        {"documentId": str(d), **processed} for d in (ready_d, empty_d, busy_d, extra_d)
    ], copy=copy)
    assert outcome == "RESCHEDULED"  # the busy page is looked at again shortly
    assert _page_status(journey, ready_q) == "READY"
    assert _page_status(journey, empty_q) == "NEEDS_REVIEW"
    assert _page_status(journey, busy_q) == "SYNCING_TO_AUDIT_CORE"
    assert _page_status(journey, extra_q) == "SUPPORTING"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        reason = connection.execute(
            text("SELECT status_reason FROM auditcore.p2_document_queue WHERE queue_id=:q"), {"q": empty_q},
        ).scalar_one()
    assert reason == worker._NOTHING_READ_REASON


def test_recheck_copies_values_for_nothing_read_pages_without_re_uploading(journey):
    from starlette.requests import Request

    from audit_core.uc03_p2_deal_actions import recheck_journey

    _, queue_id, _ = add_page(journey, status="NEEDS_REVIEW", submitted_minutes_ago=20)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    with journey.engine.begin() as connection:
        recheck_journey(
            tenant_id=journey.tenant_id, journey_id=journey.journey_id, request=request,
            human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
            connection=connection,
        )
    assert _page_status(journey, queue_id) == "SYNCING_TO_AUDIT_CORE"
    assert queue_row(journey, "JOURNEY_RECONCILE", str(journey.journey_id))["work_status"] == "PENDING"
    assert queue_row(journey, "DOCUMENT_INGEST", str(queue_id)) is None  # never sent to DI again


def test_nightly_sweep_recopies_pages_the_old_timer_marked_nothing_read(journey):
    """Task #48 (2026-09-30): the nightly copy safety net. A page DI read
    but Audit Core holds no values for goes back for one copy: marked
    "nothing read" by the old timer, or READY with its values gone. A page
    the worker itself read as empty, and a READY page whose values are
    held, are left alone."""
    _, legacy_q, _ = add_page(journey, page_number=1, status="NEEDS_REVIEW")
    _, truly_empty_q, _ = add_page(journey, page_number=2, status="NEEDS_REVIEW")
    _, held_q, held_d = add_page(journey, page_number=3, status="READY")
    _, lost_q, _ = add_page(journey, page_number=4, status="READY")
    add_extracted_field(journey, di_document_id=held_d, field_key="customer_name", value="A", confidence=0.9)
    for queue_id in (legacy_q, truly_empty_q, held_q, lost_q):
        _set_page_age(journey, queue_id, hours=25)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET status_reason=:r WHERE queue_id=:q"),
            {"q": truly_empty_q, "r": worker._NOTHING_READ_REASON},
        )
        worker.queue_nightly_upload_sweep(connection, tenant_id=journey.tenant_id)
    assert _page_status(journey, legacy_q) == "SYNCING_TO_AUDIT_CORE"
    assert _page_status(journey, truly_empty_q) == "NEEDS_REVIEW"
    assert _page_status(journey, lost_q) == "SYNCING_TO_AUDIT_CORE"
    assert _page_status(journey, held_q) == "READY"
    assert queue_row(journey, "JOURNEY_RECONCILE", str(journey.journey_id))["work_status"] == "PENDING"


def test_a_classified_type_outside_the_catalogue_gets_a_repeatable_checklist_row(journey):
    """Decision 2026-10-01: a KYC form or a UPI screenshot has no catalogue
    row, so it had no requirement ref and its values could never be stored.
    The worker creates an OPTIONAL, repeatable row for it on demand; a type
    the catalogue already lists gets nothing; a second call creates nothing."""
    _, kyc_queue, _ = add_page(journey, page_number=1, status="EXTRACTING")
    _, docket_queue, _ = add_page(journey, page_number=2, status="EXTRACTING")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET template_key='customer_kyc' WHERE queue_id=:q"),
            {"q": kyc_queue},
        )
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET template_key='booking_docket' WHERE queue_id=:q"),
            {"q": docket_queue},
        )
        connection.execute(
            text("""INSERT INTO auditcore.journey_document_requirements (
                        tenant_id, journey_id, document_requirement_item_id, requirement_key,
                        document_type_key, process_area, requirement_level, requirement_status, condition_snapshot)
                    VALUES (:t, :j, NULL, 'BOOKING_DOCKET', 'booking_form', 'BOOKING', 'REQUIRED', 'PENDING', '{}'::jsonb)
                    ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING"""),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
        assert worker._ensure_extra_requirement_rows(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
        ) == 1
        rows = connection.execute(
            text("SELECT requirement_key, document_type_key, process_area, requirement_level "
                 "FROM auditcore.journey_document_requirements WHERE tenant_id=:t AND journey_id=:j "
                 "AND requirement_key LIKE 'p2_extra_%'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().all()
        assert [tuple(r.values()) for r in rows] == [("p2_extra_customer_kyc", "customer_kyc", "BOOKING", "OPTIONAL")]
        assert worker._ensure_extra_requirement_rows(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
        ) == 0


def test_a_field_with_a_correction_waiting_for_the_team_lead_cannot_be_edited_or_confirmed_again(journey):
    from fastapi import HTTPException

    from audit_core.uc03_p2_documents import (
        P2FieldConfirmCommand,
        _pending_corrections,
        _pending_for,
        confirm_p2_document_field,
    )

    di_document_id = uuid4()
    add_page(journey, di_document_id=di_document_id)
    canonical = add_extracted_field(
        journey, di_document_id=di_document_id, field_key="p2_test_amount", value="100", confidence=98.0,
    )

    def propose(new_value):
        with journey.engine.begin() as connection:
            return correct_p2_document_field(
                tenant_id=journey.tenant_id, journey_id=journey.journey_id, document_id=di_document_id,
                command=P2FieldCorrectionCommand(
                    canonicalFieldId=canonical, fieldKey="p2_test_amount",
                    sourceFactVersion=1, newValue=new_value, remarks="wrong year captured",
                ),
                human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
                connection=connection,
            )

    def pending_on_review():
        """What the document review shows on the field: the open proposal, if any."""
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            open_proposals = _pending_corrections(
                connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, document_id=di_document_id,
            )
        return _pending_for(
            open_proposals, field_key="p2_test_amount", canonical_field_id=canonical, source_fact_version=1,
        )

    assert pending_on_review() is None
    first = propose("150")
    waiting = pending_on_review()
    assert waiting["taskId"] == first["taskId"] and waiting["proposedValue"] == "150"

    with pytest.raises(HTTPException) as again:
        propose("175")
    assert again.value.status_code == 409 and "waiting for the Team Lead" in again.value.detail
    with journey.engine.begin() as connection, pytest.raises(HTTPException) as confirm:
        confirm_p2_document_field(
            tenant_id=journey.tenant_id, journey_id=journey.journey_id, document_id=di_document_id,
            field_key="p2_test_amount",
            command=P2FieldConfirmCommand(canonicalFieldId=canonical, sourceFactVersion=1),
            human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
            connection=connection,
        )
    assert confirm.value.status_code == 409

    # Once the Team Lead rejects it, the PC can correct the field again.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        submit_action(
            connection, tenant_id=journey.tenant_id, task_id=first["taskId"],
            action="REJECT_CORRECTION", actor_id="tl-1", actor_role_code="TL", comment="Year is right",
        )
    assert pending_on_review() is None
    assert propose("175")["applied"] is False


# ------------------------------------- opening a page DI never classified

def _review_with_facts_error(journey, monkeypatch, *, queue_status, template_key, overridden, code):
    from types import SimpleNamespace

    from audit_core import uc03_p2_documents as documents
    from audit_core.di_client import DiClientError, DiDocument

    di_document_id = uuid4()
    add_page(journey, di_document_id=di_document_id, status=queue_status)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET template_key=:k, "
                 "type_overridden_by_actor_id=:o WHERE tenant_id=:t AND di_document_id=:d"),
            {"k": template_key, "o": journey.actor_id if overridden else None,
             "t": journey.tenant_id, "d": di_document_id},
        )
    monkeypatch.setattr(documents, "_ensure_di_context", lambda **_: ("ctx", "token"))

    class Di:
        def get_audit_document(self, **_):
            return DiDocument(document_id=str(di_document_id), upload_status="UPLOADED",
                              processing_status="PROCESSED", confirmation_status="UNCONFIRMED")

        def get_audit_document_facts(self, **_):
            raise DiClientError(status_code=422, code=code, retryable=False)

    return documents.get_p2_document_review(
        tenant_id=journey.tenant_id, journey_id=journey.journey_id, document_id=di_document_id,
        human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
        engine=journey.engine,
        security_client=SimpleNamespace(get_service_token=lambda **_: "token"),
        di_client=Di(),
    )


def test_a_page_awaiting_its_type_opens_with_its_preview_instead_of_an_error(journey, monkeypatch):
    review = _review_with_facts_error(
        journey, monkeypatch, queue_status="SUPPORTING", template_key="supporting_document",
        overridden=False, code="E008",
    )
    assert review["fields"] == []
    assert review["contentAvailable"] is True
    assert review["diReadError"] is None


def test_a_page_not_read_yet_opens_with_its_preview_instead_of_an_error(journey, monkeypatch):
    # Product owner 2026-10-20: a receipt whose reading failed (waiting for a
    # retry) said "could not be loaded ... VAC-SYS-002" when opened.
    for queue_status, template_key, overridden in (
        ("SUPPORTING", "booking_form", False),          # classified, not read yet
        ("SUPPORTING", "supporting_document", True),    # PC already typed it (Others)
        ("READY", "dealer_receipt", False),             # reading failed, retry pending
    ):
        review = _review_with_facts_error(
            journey, monkeypatch, queue_status=queue_status, template_key=template_key,
            overridden=overridden, code="E008",
        )
        assert review["fields"] == []
        assert review["contentAvailable"] is True
        assert review["diReadError"] is None


def test_a_real_di_failure_still_reports_the_dependency_error(journey, monkeypatch):
    from audit_core.errors import DependencyUnavailableError

    with pytest.raises(DependencyUnavailableError):
        _review_with_facts_error(
            journey, monkeypatch, queue_status="READY", template_key="dealer_receipt",
            overridden=False, code="DI_UNAVAILABLE",
        )


def test_a_normal_document_review_returns_fields_with_the_correction_waiting_for_the_team_lead(journey, monkeypatch):
    from types import SimpleNamespace

    from audit_core import uc03_p2_documents as documents
    from audit_core.di_client import DiDocument, DiFact

    di_document_id = uuid4()
    add_page(journey, di_document_id=di_document_id, status="READY")
    canonical = add_extracted_field(
        journey, di_document_id=di_document_id, field_key="p2_test_amount", value="100", confidence=98.0,
    )
    with journey.engine.begin() as connection:
        proposal = correct_p2_document_field(
            tenant_id=journey.tenant_id, journey_id=journey.journey_id, document_id=di_document_id,
            command=P2FieldCorrectionCommand(
                canonicalFieldId=canonical, fieldKey="p2_test_amount",
                sourceFactVersion=1, newValue="150", remarks="wrong year captured",
            ),
            human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
            connection=connection,
        )
    monkeypatch.setattr(documents, "_ensure_di_context", lambda **_: ("ctx", "token"))

    class Di:
        def get_audit_document(self, **_):
            return DiDocument(document_id=str(di_document_id), upload_status="UPLOADED",
                              processing_status="PROCESSED", confirmation_status="CONFIRMED")

        def get_audit_document_facts(self, **_):
            return (DiFact(canonical_field_id=canonical, field_key="p2_test_amount", value="100",
                           value_source="EXTRACTED", confidence_score=98.0, version_no=1),)

    review = documents.get_p2_document_review(
        tenant_id=journey.tenant_id, journey_id=journey.journey_id, document_id=di_document_id,
        human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
        engine=journey.engine,
        security_client=SimpleNamespace(get_service_token=lambda **_: "token"),
        di_client=Di(),
    )
    field = next(f for f in review["fields"] if f["fieldKey"] == "p2_test_amount")
    assert field["pendingCorrection"]["taskId"] == proposal["taskId"]
    assert field["pendingCorrection"]["proposedValue"] == "150"
    assert review["diReadError"] is None and review["contentAvailable"] is True
