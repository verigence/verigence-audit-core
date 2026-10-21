"""Retry rule of 2026-10-21 (product owner, 12:24 IST; replacing the night
attempt of 2026-10-20) and the review fixes it keeps (real Postgres):

- a retryable failure is tried at T0, T0+10 and T0+20 minutes, then FAILED;
- a refusal fails at once;
- no file takes more than 30 minutes (fail_unsettled_pages);
- every failed page raises the file's "Document to resubmit" task
  (category DOCUMENT_RESUBMIT) with the reason and a link to the page;
- the failure report lists every failed page with its link;
- our own setup failure: no need to re-upload, the Team Lead is alerted;
- F1: no second DI document for one page; CONFLICT adoption; no silent
  dead letters; durable once-only claims."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_batch_pages,
    add_page,
    create_p2_journey,
    database_engine,
    queue_row,
)
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.di_capture_v2_client import DiCaptureV2Error
from audit_core.uc03_p2_runtime import claim_scheduled_run, enqueue_work

SETUP = "DOCUMENT_AI_NOT_CONFIGURED"
_AI_FAILED = {"state": "CLASSIFIED", "processingStatus": "FAILED", "processingFailureCode": "DOCUMENT_AI_UNAVAILABLE",
              "failureKind": "AI", "scanPassed": True, "retryTonight": False}


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine)
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


class FakeClient:
    def __init__(self, documents=None, *, fail_listing=False, intents=None):
        self.documents = documents or []
        self.fail_listing = fail_listing
        self.intents = intents
        self.deleted: list[str] = []

    def list_documents(self, *, token, tenant_id, external_context_ref, phase):
        if self.fail_listing:
            raise worker.DiCaptureV2Error(status_code=504, detail="DI V2 request failed: ReadTimeout")
        return {"documents": self.documents if phase == "BOOKING" else []}

    def create_upload_intents(self, **_kwargs):
        return self.intents

    def delete_document(self, *, token, tenant_id, external_context_ref, document_id):
        self.deleted.append(document_id)


class FakeStorage:
    def presign_get(self, object_key, *, expires_seconds=1800):
        return f"https://signed.example/{object_key}?expires={expires_seconds}"


def _sql(journey, statement, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        result = connection.execute(text(statement), {"t": journey.tenant_id, "j": journey.journey_id, **params})
        return result.mappings().all() if result.returns_rows else None


def _page(journey, queue_id):
    [row] = _sql(journey, "SELECT queue_status, status_reason, last_error, di_document_id, client_upload_id, "
                          "attempt_count, read_clock_started_at_utc FROM auditcore.p2_document_queue "
                          "WHERE queue_id=:q", q=queue_id)
    return row


def _open_tasks(journey):
    return _sql(journey, "SELECT task_type, category, severity, title, description, dedupe_key, reference "
                         "FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j AND task_status='READY' "
                         "ORDER BY dedupe_key")


def _sync_tasks(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker.sync_processing_failure_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _sweep(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return worker.queue_nightly_upload_sweep(connection, tenant_id=journey.tenant_id)


def _watchdog(journey, now=None):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return worker.fail_unsettled_pages(connection, tenant_id=journey.tenant_id, now=now)


def _uploaded_minutes_ago(journey, batch_id, minutes):
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET created_at_utc=now() - (:m * interval '1 minute') "
                  "WHERE tenant_id=:t AND batch_id=:b", b=batch_id, m=minutes)


def _run_ingest(journey, monkeypatch, client):
    monkeypatch.setattr(worker, "_di_context_and_requirements", lambda engine, work: ("ctx", "tok", [], {}))
    monkeypatch.setattr(worker, "get_di_capture_v2_client", lambda: client)
    items = [i for i in worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
             if i.work_type == "DOCUMENT_INGEST"]
    assert items, "no ingest work was due"
    for item in items:
        try:
            worker._ingest_document(journey.engine, item)
        except Exception as exc:  # noqa: BLE001 - the worker's own failure path
            worker._fail(journey.engine, item, exc)
        else:
            worker._complete(journey.engine, item)
    return items


def _due_now(journey, work_type, key):
    _sql(journey, "UPDATE auditcore.p2_work_queue SET next_attempt_at_utc=now() "
                  "WHERE tenant_id=:t AND work_type=:w AND work_key=:k", w=work_type, k=key)


def _api(journey, method, path, *, subject=None):
    from audit_core.dependencies import get_human_principal
    from audit_core.main import app
    from audit_core.security import HumanPrincipal
    from audit_core.security_authorization import get_security_authorization_client

    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=subject or journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        client = TestClient(app, raise_server_exceptions=False)
        return getattr(client, method)(f"/p2/v1/tenants/{journey.tenant_id}{path}")
    finally:
        app.dependency_overrides.clear()


def _retry(journey, queue_id):
    return _api(journey, "post", f"/journeys/{journey.journey_id}/pages/{queue_id}:retry")


# ------------------------------------------------------------------- T0 / +10 / +20, then FAILED

def test_the_document_steps_wait_10_then_10_minutes_three_attempts_in_all():
    for work_type in ("DOCUMENT_INGEST", "SPLIT_BATCH", "PAGE_REREAD", "BATCH_GROUP"):
        assert worker._max_attempts(work_type) == 3
        assert [worker._retry_delay(work_type, n) for n in (1, 2)] == [600, 600]


def test_a_page_the_document_service_cannot_take_is_tried_at_t0_10_and_20_minutes_then_resubmitted(journey):
    down = DiCaptureV2Error(status_code=503, detail="down")
    batch_id, queue_id, _ = add_page(journey, status="DI_UPLOAD_PREPARING")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type="DOCUMENT_INGEST", work_key=str(queue_id), payload={}, correlation_id=None)
    for attempt in (1, 2):
        [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
        worker._fail(journey.engine, item, down)
        row = queue_row(journey, "DOCUMENT_INGEST", str(queue_id))
        assert row["work_status"] == "RETRY_WAIT" and row["attempt_count"] == attempt
        [wait] = _sql(journey, "SELECT EXTRACT(EPOCH FROM next_attempt_at_utc - updated_at_utc) AS s "
                               "FROM auditcore.p2_work_queue WHERE work_id=:w", w=row["work_id"])
        assert 595 <= float(wait["s"]) <= 605  # T0+10, then T0+20
        page = _page(journey, queue_id)
        assert page["queue_status"] == "RETRY_WAIT"
        assert page["status_reason"] == ("Document service returned an error — trying again in 10 minutes "
                                         f"(attempt {attempt} of 3). Nothing to do yet.")
        assert _open_tasks(journey) == []  # nothing for the PC while it retries
        _due_now(journey, "DOCUMENT_INGEST", str(queue_id))
    [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    worker._fail(journey.engine, item, down)  # the attempt at T0+20 fails too
    assert queue_row(journey, "DOCUMENT_INGEST", str(queue_id))["work_status"] == "DEAD_LETTER"
    page = _page(journey, queue_id)
    assert page["queue_status"] == "FAILED" and page["attempt_count"] == 3
    assert page["status_reason"].startswith("Document service returned an error — tried 3 times (at once, then "
                                            "10 and 20 minutes later).")
    [task] = _open_tasks(journey)
    assert task["task_type"] == "DOCUMENT_RESUBMIT" and task["category"] == "DOCUMENT_RESUBMIT"
    assert task["dedupe_key"] == f"resubmit:{journey.journey_id}:{batch_id}"
    assert task["title"] == "scan.pdf: 1 page to resubmit" and task["severity"] == "HIGH"
    [entry] = task["reference"]["pages"]
    assert entry["queueId"] == str(queue_id) and entry["pageNumbers"] == [1]
    assert entry["filePath"] == f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/pages/{queue_id}/file"
    assert entry["reason"] == page["status_reason"]


def test_di_retrying_a_page_is_waited_for_and_its_final_failure_says_how_often_it_was_tried(journey, monkeypatch):
    from test_uc03_p2_runtime import _reconcile

    _, queue_id, document_id = add_page(journey, status="EXTRACTING")
    retrying = {**_AI_FAILED, "documentId": str(document_id), "processingStatus": "RETRY_PENDING",
                "nextAttemptAtUtc": "2026-10-21T07:10:00+00:00", "attemptsMade": 1}
    _reconcile(journey, monkeypatch, [retrying])
    page = _page(journey, queue_id)
    assert page["queue_status"] == "EXTRACTING"
    assert page["status_reason"] == ("AI reading failed — it is tried again 10 and 20 minutes after the first try. "
                                     "Nothing to do now.")
    assert _open_tasks(journey) == []
    _reconcile(journey, monkeypatch, [{**_AI_FAILED, "documentId": str(document_id), "attemptsMade": 3}])
    page = _page(journey, queue_id)
    assert page["queue_status"] == "FAILED" and page["attempt_count"] == 3
    assert page["last_error"] == "DOCUMENT_AI_UNAVAILABLE"
    assert page["status_reason"] == ("AI reading failed — tried 3 times (at once, then 10 and 20 minutes later). "
                                     "Please upload this page again.")
    assert [t["task_type"] for t in _open_tasks(journey)] == ["DOCUMENT_RESUBMIT"]


@pytest.mark.parametrize(("item", "reason"), [
    ({"state": "FAILED", "failureCode": "DI_QUALITY_IMAGE_BLUR_SCORE", "failureKind": "DOCUMENT",
      "scanPassed": False, "failureDetail": "the image is blurred"},
     "Page rejected: the image is blurred. Re-scan this page and upload it again; retrying will not help."),
    ({"state": "FAILED", "failureCode": "CORRUPT", "failureKind": "DOCUMENT", "scanPassed": False},
     "File couldn't be scanned — please reload it."),
    ({"state": "CLASSIFIED", "processingStatus": "FAILED", "processingFailureCode": "DOCUMENT_AI_CONTENT_BLOCKED",
      "failureKind": "AI", "scanPassed": True},
     "AI couldn't read this page — please upload it again."),
])
def test_a_refusal_fails_at_once(item, reason):
    outcome = worker.classify_page_outcome(di_item=item, extracted_count=0, submitted_at=None, facts_copy=None,
                                           now=datetime.now(UTC))
    assert (outcome.status, outcome.reason) == ("FAILED", reason)


def test_a_dead_read_again_fails_the_page_for_the_pc(journey):
    _, queue_id, _ = add_page(journey, status="NEEDS_REVIEW")
    _dead(journey, "PAGE_REREAD", str(queue_id))
    page = _page(journey, queue_id)
    assert page["queue_status"] == "FAILED" and '"Read again" could not be sent' in page["status_reason"]
    assert [t["task_type"] for t in _open_tasks(journey)] == ["DOCUMENT_RESUBMIT"]


# ------------------------------------------------------------------- the 30-minute ceiling

def test_a_page_not_settled_30_minutes_after_upload_is_failed_and_resubmitted(journey):
    batch_id, polling, _ = add_page(journey, page_number=1, status="EXTRACTING")
    _, waiting_retry, _ = add_page(journey, page_number=2, status="RETRY_WAIT")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type="DOCUMENT_INGEST", work_key=str(waiting_retry), payload={}, delay_seconds=300,
                     correlation_id=None)
    _sql(journey, "UPDATE auditcore.p2_document_queue SET batch_id=:b WHERE queue_id=:q", b=batch_id, q=waiting_retry)
    _uploaded_minutes_ago(journey, batch_id, 29)
    assert _watchdog(journey) == 0  # 29 minutes: still inside the ceiling
    assert _page(journey, polling)["queue_status"] == "EXTRACTING"
    _uploaded_minutes_ago(journey, batch_id, 31)
    assert _watchdog(journey) == 2
    for queue_id in (polling, waiting_retry):
        page = _page(journey, queue_id)
        assert page["queue_status"] == "FAILED" and page["last_error"] == "NOT_READ_IN_30_MIN"
        assert page["status_reason"] == "Not read within 30 minutes. Please upload this page again."
    # the retry that was still to come is not sent any more
    assert queue_row(journey, "DOCUMENT_INGEST", str(waiting_retry))["work_status"] == "CANCELLED"
    [status] = _sql(journey, "SELECT batch_status FROM auditcore.p2_upload_batches WHERE batch_id=:b", b=batch_id)
    assert status["batch_status"] == "FAILED"
    [task] = _open_tasks(journey)
    assert task["task_type"] == "DOCUMENT_RESUBMIT" and task["title"] == "scan.pdf: 2 pages to resubmit"
    assert "Page 1: Not read within 30 minutes." in task["description"]
    assert sorted(p["queueId"] for p in task["reference"]["pages"]) == sorted([str(polling), str(waiting_retry)])
    assert _watchdog(journey) == 0  # settled once


def test_the_ceiling_leaves_read_pages_and_restarts_with_a_retry(journey):
    batch_id, syncing, _ = add_page(journey, page_number=1, status="SYNCING_TO_AUDIT_CORE")
    _, retried, _ = add_page(journey, page_number=2, status="CLASSIFYING")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET batch_id=:b WHERE queue_id=:q", b=batch_id, q=retried)
    _uploaded_minutes_ago(journey, batch_id, 120)
    # A person pressed Retry 5 minutes ago: the page's 30 minutes start again.
    _sql(journey, "UPDATE auditcore.p2_document_queue SET read_clock_started_at_utc=now() - interval '5 minutes' "
                  "WHERE queue_id=:q", q=retried)
    assert _watchdog(journey) == 0
    assert _page(journey, syncing)["queue_status"] == "SYNCING_TO_AUDIT_CORE"  # DI read it: Audit Core copies
    assert _watchdog(journey, now=datetime.now(UTC) + timedelta(minutes=26)) == 1
    assert _page(journey, retried)["last_error"] == "NOT_READ_IN_30_MIN"


def test_retry_and_read_again_restart_the_pages_clock(journey):
    _, queue_id, _ = add_page(journey, status="FAILED")
    assert _page(journey, queue_id)["read_clock_started_at_utc"] is None  # runs from the upload
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker.request_page_recovery(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                     queue_id=queue_id, marker="r", requested_by="pc", correlation_id=None)
    assert _page(journey, queue_id)["read_clock_started_at_utc"] is not None
    _, other, _ = add_page(journey, page_number=2, status="READY")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker.request_page_reread(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                   queue_id=other, requested_by="pc", correlation_id=None)
    assert _page(journey, other)["read_clock_started_at_utc"] is not None


@pytest.mark.parametrize(("status", "reason"), [
    ("AWAITING_UPLOAD", "The file did not finish uploading within 30 minutes. Please upload the file again."),
    ("SPLITTING", "The file was not split into pages within 30 minutes. Please upload the file again."),
])
def test_a_file_not_uploaded_or_split_within_30_minutes_is_failed(journey, status, reason):
    batch_id = uuid4()
    _sql(journey, "INSERT INTO auditcore.p2_upload_batches (tenant_id, batch_id, journey_id, original_filename, "
                  "content_type, size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id, "
                  "created_at_utc) VALUES (:t, :b, :j, 'big.pdf', 'application/pdf', 100, 0, 'k', :s, 'pc', "
                  "now() - interval '31 minutes')", b=batch_id, s=status)
    assert _watchdog(journey) == 1
    [batch] = _sql(journey, "SELECT batch_status, failure_reason FROM auditcore.p2_upload_batches WHERE batch_id=:b",
                   b=batch_id)
    assert (batch["batch_status"], batch["failure_reason"]) == ("FAILED", reason)
    [task] = _open_tasks(journey)
    assert task["task_type"] == "DOCUMENT_RESUBMIT" and task["title"] == "big.pdf: upload the file again"
    assert task["description"] == f"big.pdf: {reason}"


def test_the_ceiling_keeps_the_reason_of_a_page_our_setup_refused(journey):
    """The night re-read of a page our setup refused that runs out of time
    goes back to FAILED as it was: still our problem, still no re-upload."""
    _, queue_id, _ = add_page(journey, status="QUEUED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error=:e, status_reason=:r, "
                  "read_clock_started_at_utc=now() - interval '31 minutes' WHERE queue_id=:q",
         q=queue_id, e=SETUP, r=worker._SETUP_REASON)
    assert _watchdog(journey) == 1
    page = _page(journey, queue_id)
    assert (page["queue_status"], page["last_error"], page["status_reason"]) == ("FAILED", SETUP, worker._SETUP_REASON)


def test_the_worker_loop_runs_the_ceiling(journey, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "fail_unsettled_pages", lambda connection, **kw: calls.append(kw) or 0)
    monkeypatch.setattr(worker, "_fact_sweep", lambda engine, tenant_id: 0)
    monkeypatch.setattr(worker, "backfill_price_variance", lambda engine, tenant_id: 0)
    monkeypatch.setattr(worker, "_NIGHTLY_REVIEW_UTC", "23:59")
    worker._last_sweep_at.pop(journey.tenant_id, None)
    worker._last_watchdog_at.pop(journey.tenant_id, None)
    worker._maybe_sweep(journey.engine, journey.tenant_id)
    assert calls == [{"tenant_id": journey.tenant_id}]


# ------------------------------------------------------------------- the resubmit task

def test_one_resubmit_task_per_file_listing_each_failed_page(journey):
    batch_id, first, _ = add_page(journey, page_number=1, status="FAILED")
    _, second, _ = add_page(journey, page_number=2, status="FAILED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET batch_id=:b WHERE queue_id=:q", b=batch_id, q=second)
    _sql(journey, "UPDATE auditcore.p2_document_queue SET status_reason='AI couldn''t read this page — please "
                  "upload it again.', last_error='DOCUMENT_AI_CONTENT_BLOCKED' WHERE queue_id=:q", q=first)
    _sql(journey, "UPDATE auditcore.p2_document_queue SET status_reason=:r, last_error='NOT_READ_IN_30_MIN' "
                  "WHERE queue_id=:q", q=second, r=worker.NOT_READ_IN_TIME_REASON)
    _sync_tasks(journey)
    [task] = _open_tasks(journey)
    assert task["title"] == "scan.pdf: 2 pages to resubmit"
    assert task["description"].startswith("Page 1: AI couldn't read this page — please upload it again. "
                                          "Page 2: Not read within 30 minutes. Please upload this page again.")
    assert task["reference"]["category"] == "DOCUMENT_RESUBMIT" and task["reference"]["pageNumbers"] == [1, 2]
    assert [p["code"] for p in task["reference"]["pages"]] == ["DOCUMENT_AI_CONTENT_BLOCKED", "NOT_READ_IN_30_MIN"]
    # Page 2 is read after a Retry: it leaves the task; page 1 uploaded again (superseded): the task closes.
    _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='READY' WHERE queue_id=:q", q=second)
    _sync_tasks(journey)
    [task] = _open_tasks(journey)
    assert task["title"] == "scan.pdf: 1 page to resubmit"
    _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='CANCELLED', last_error='SUPERSEDED_PAGE' "
                  "WHERE queue_id=:q", q=first)
    _sync_tasks(journey)
    assert _open_tasks(journey) == []


def test_a_file_with_failed_pages_may_be_uploaded_again_whole(journey):
    """The same file uploaded again is refused as a duplicate unless some of
    its pages failed: then it is the resubmission."""
    batch_id, _, _ = add_page(journey, status="FAILED")
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET sha256='abc', batch_status='PARTIAL_FAILURE' "
                  "WHERE batch_id=:b", b=batch_id)
    again, _, _ = add_page(journey, page_number=1, status="QUEUED")
    item = worker.WorkItem(tenant_id=journey.tenant_id, work_id=uuid4(), journey_id=journey.journey_id,
                           work_type="SPLIT_BATCH", work_key=str(again), payload={}, attempt_count=0,
                           requested_version=None, correlation_id=None, lease_token=uuid4())
    assert worker._refuse_duplicate_upload(journey.engine, item, batch_id=again, digest="abc") is False
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET batch_status='COMPLETED' WHERE batch_id=:b", b=batch_id)
    assert worker._refuse_duplicate_upload(journey.engine, item, batch_id=again, digest="abc") is True


def test_our_setup_problem_tells_the_pc_no_need_to_re_upload_and_alerts_the_team_lead(journey):
    _, queue_id, _ = add_page(journey, status="FAILED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error=:e, status_reason=:r WHERE queue_id=:q",
         q=queue_id, e=SETUP, r=worker._SETUP_REASON)
    _sync_tasks(journey)
    resubmit, alert = sorted(_open_tasks(journey), key=lambda t: t["task_type"])
    assert alert["task_type"] == "TL_DOCUMENT_STUCK" and "refused our credentials" in alert["title"]
    assert resubmit["task_type"] == "DOCUMENT_RESUBMIT" and resubmit["severity"] == "INFO"
    assert resubmit["title"] == "scan.pdf: page 1 not read — our setup problem, no need to re-upload"
    assert resubmit["description"] == ("Page 1: Our setup problem — will be re-read once fixed; no need to "
                                       "re-upload. Your Team Lead has been alerted.")
    assert resubmit["reference"]["setupOnly"] is True


def test_the_night_looks_only_at_pages_our_setup_refused(journey, monkeypatch):
    _, setup_page, document_id = add_page(journey, page_number=1, status="FAILED")
    _, ai_page, _ = add_page(journey, page_number=2, status="FAILED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error=:e, status_reason=:r WHERE queue_id=:q",
         q=setup_page, e=SETUP, r=worker._SETUP_REASON)
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='DOCUMENT_AI_UNAVAILABLE' WHERE queue_id=:q",
         q=ai_page)
    assert _sweep(journey)["retried"] == 1
    assert _page(journey, ai_page)["queue_status"] == "FAILED"  # the PC's to resubmit, never sent again
    page = _page(journey, setup_page)
    assert page["queue_status"] == "QUEUED" and page["last_error"] == SETUP  # kept while it is looked up
    # DI's night re-read read it: copied.
    _run_ingest(journey, monkeypatch, FakeClient([{"documentId": str(document_id), "state": "CLASSIFIED",
                                                   "processingStatus": "PROCESSED"}]))
    assert _page(journey, setup_page)["queue_status"] == "SYNCING_TO_AUDIT_CORE"


def test_a_di_hiccup_during_the_nightly_lookup_keeps_the_setup_reason(journey, monkeypatch):
    _, queue_id, _ = add_page(journey, status="FAILED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error=:e, status_reason=:r WHERE queue_id=:q",
         q=queue_id, e=SETUP, r=worker._SETUP_REASON)
    _sweep(journey)
    _run_ingest(journey, monkeypatch, FakeClient(fail_listing=True))
    row = queue_row(journey, "DOCUMENT_INGEST", str(queue_id))
    assert row["work_status"] == "RETRY_WAIT" and row["attempt_count"] == 1
    page = _page(journey, queue_id)
    assert (page["queue_status"], page["last_error"], page["status_reason"]) == ("QUEUED", SETUP, worker._SETUP_REASON)
    # It keeps failing: the page goes back to FAILED as it was and the next night looks again.
    _sql(journey, "UPDATE auditcore.p2_work_queue SET attempt_count=12, next_attempt_at_utc=now() "
                  "WHERE tenant_id=:t AND work_type='DOCUMENT_INGEST' AND work_key=:k", k=str(queue_id))
    _run_ingest(journey, monkeypatch, FakeClient(fail_listing=True))
    page = _page(journey, queue_id)
    assert (page["queue_status"], page["last_error"]) == ("FAILED", SETUP)
    assert _sweep(journey)["retried"] == 1


# ------------------------------------------------------------------- the failure report and the page link

def _tl(journey) -> str:
    tl = f"tl-{uuid4().hex[:8]}"
    with journey.engine.begin() as connection:
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                 "VALUES (:t, :a, 'TL')"),
            {"t": journey.tenant_id, "a": tl},
        )
    return tl


def test_the_failure_report_lists_every_failed_page_with_a_link(journey, monkeypatch):
    import audit_core.uc03_p2_storage as storage

    monkeypatch.setattr(storage, "get_p2_document_storage", lambda: FakeStorage())
    tl = _tl(journey)
    batch_id, failed, _ = add_page(journey, page_number=2, status="EXTRACTING")
    add_page(journey, page_number=1, status="READY")
    _uploaded_minutes_ago(journey, batch_id, 40)
    _watchdog(journey)
    response = _api(journey, "get", "/reports/failed-pages", subject=tl)
    assert response.status_code == 200, response.text
    body = response.json()
    [row] = body["rows"]
    assert row["kind"] == "PAGE" and row["queueId"] == str(failed) and row["pageNumbers"] == [2]
    assert row["journeyId"] == str(journey.journey_id) and row["filename"] == "scan.pdf"
    assert row["reason"] == "Not read within 30 minutes. Please upload this page again."
    assert row["code"] == "NOT_READ_IN_30_MIN" and row["setupProblem"] is False
    assert row["uploadedAtUtc"] < row["failedAtUtc"]
    assert row["openUrl"] == f"https://signed.example/p2/{batch_id}/2?expires=1800"  # presigned, never the bucket
    assert row["filePath"].endswith(f"/journeys/{journey.journey_id}/pages/{failed}/file")
    assert body["truncated"] is False and body["from"] == body["to"]
    # A day with no failures lists none; a PC may not read the report.
    assert _api(journey, "get", "/reports/failed-pages?from=2026-01-01&to=2026-01-02", subject=tl).json()["rows"] == []
    assert _api(journey, "get", "/reports/failed-pages").status_code == 403
    assert _api(journey, "get", "/reports/failed-pages?from=2026-02-01&to=2026-01-01", subject=tl).status_code == 422


def test_the_daily_processing_report_carries_the_failed_pages(journey, monkeypatch):
    import audit_core.uc03_p2_storage as storage
    from audit_core.uc03_p2_daily_report import build_daily_processing_report

    monkeypatch.setattr(storage, "get_p2_document_storage", lambda: FakeStorage())
    tl = _tl(journey)
    _, queue_id, _ = add_page(journey, status="FAILED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET status_reason='AI couldn''t read this page — please "
                  "upload it again.', last_error='DOCUMENT_AI_CONTENT_BLOCKED' WHERE queue_id=:q", q=queue_id)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        day = connection.execute(text("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date")).scalar_one()
        report = build_daily_processing_report(connection, tenant_id=journey.tenant_id, actor_id=tl, day=day)
    [row] = report["failedPages"]
    assert row["queueId"] == str(queue_id) and row["reason"] == "AI couldn't read this page — please upload it again."
    assert row["openUrl"].startswith("https://signed.example/")


def test_the_page_link_issues_a_short_lived_storage_link(journey, monkeypatch):
    import audit_core.uc03_p2_api as api

    monkeypatch.setattr(api, "get_p2_document_storage", lambda: FakeStorage())
    batch_id, queue_id, _ = add_page(journey, status="FAILED")
    response = _api(journey, "get", f"/journeys/{journey.journey_id}/pages/{queue_id}/file")
    assert response.status_code == 200, response.text
    assert response.json() == {"queueId": str(queue_id), "pageNumbers": [1], "status": "FAILED",
                               "url": f"https://signed.example/p2/{batch_id}/1?expires=1800",
                               "expiresInSeconds": 1800}
    assert _api(journey, "get", f"/journeys/{journey.journey_id}/pages/{uuid4()}/file").status_code == 404


# ------------------------------------------------------------------- F1: one DI document per page

def _config_page(journey, *, di_state):
    _, queue_id, document_id = add_page(journey, status="FAILED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error=:e, di_state=:s WHERE queue_id=:q",
         q=queue_id, s=di_state, e=SETUP)
    return queue_id, document_id


def test_retry_is_refused_for_a_page_our_setup_refused_while_being_identified(journey):
    queue_id, _ = _config_page(journey, di_state="FAILED")
    response = _retry(journey, queue_id)
    assert response.status_code == 409
    assert response.json()["detail"] == ("The document service will read this page overnight now that the setup "
                                         "is fixed — nothing to do.")
    assert _page(journey, queue_id)["queue_status"] == "FAILED"


def test_retry_after_the_setup_is_fixed_reads_the_page_once_more(journey, monkeypatch):
    queue_id, document_id = _config_page(journey, di_state="CLASSIFIED")
    assert _retry(journey, queue_id).status_code < 300
    _run_ingest(journey, monkeypatch, FakeClient([{
        "documentId": str(document_id), "state": "CLASSIFIED", "processingStatus": "FAILED",
        "processingFailureCode": SETUP, "failureKind": "CONFIGURATION", "scanPassed": True, "retryTonight": True}]))
    assert queue_row(journey, "PAGE_REREAD", str(queue_id))["work_status"] == "PENDING"
    assert "~" not in _page(journey, queue_id)["client_upload_id"], "no second upload"


def test_the_worker_leaves_a_classification_stage_setup_refusal_for_the_night(journey, monkeypatch):
    queue_id, document_id = _config_page(journey, di_state="FAILED")
    with journey.engine.begin() as connection:  # e.g. a Retry queued before the API refused it
        set_tenant_context(connection, journey.tenant_id)
        worker.request_page_recovery(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                     queue_id=queue_id, marker="r", requested_by="pc", correlation_id=None)
    client = FakeClient([{"documentId": str(document_id), "state": "FAILED", "failureCode": SETUP,
                          "failureKind": "CONFIGURATION", "scanPassed": True, "retryTonight": True}])
    _run_ingest(journey, monkeypatch, client)
    page = _page(journey, queue_id)
    assert (page["queue_status"], page["last_error"], page["status_reason"]) == ("FAILED", SETUP, worker._SETUP_REASON)
    assert "~" not in page["client_upload_id"] and client.deleted == []


def test_an_upload_sent_again_deletes_the_old_di_document_first(journey, monkeypatch):
    _, queue_id, document_id = add_page(journey, status="FAILED")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker.request_page_recovery(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                     queue_id=queue_id, marker="r", requested_by="pc", correlation_id=None)
    client = FakeClient([{"documentId": str(document_id), "state": "FAILED", "failureCode": "CLASSIFICATION_FAILED",
                          "failureKind": "TECHNICAL"}])
    _run_ingest(journey, monkeypatch, client)
    assert client.deleted == [str(document_id)]
    page = _page(journey, queue_id)
    assert page["client_upload_id"].endswith("~r1") and page["di_document_id"] is None


def test_a_page_with_another_attempt_queued_at_di_is_polled_never_told_as_failed():
    item = {**_AI_FAILED, "nextAttemptAtUtc": "2026-10-21T07:10:00+00:00"}
    now = datetime.now(UTC)
    assert worker.recovery_decision(di_item=item, marker="r", submitted_at=now, now=now) == "EXTRACTING"


def test_read_again_pending_says_one_attempt():
    outcome = worker.classify_page_outcome(
        di_item={**_AI_FAILED, "nextAttemptAtUtc": "2026-10-21T05:00:00+00:00", "inNightAttempt": False},
        extracted_count=0, submitted_at=None, facts_copy=None, now=datetime.now(UTC),
    )
    assert (outcome.status, outcome.reason) == ("EXTRACTING", "Being read again (one attempt). Nothing to do now.")


def test_a_configuration_gap_without_an_alert_does_not_claim_support_was_told():
    outcome = worker.classify_page_outcome(
        di_item={"state": "FAILED", "failureCode": "DOCUMENT_TYPE_NOT_FOUND", "failureKind": "CONFIGURATION",
                 "scanPassed": True, "retryTonight": False},
        extracted_count=0, submitted_at=None, facts_copy=None, now=datetime.now(UTC),
    )
    assert "support has been told" not in outcome.reason and "Team Lead" in outcome.reason


# ------------------------------------------------------------------- deploy step and durable claims

def test_the_one_time_deploy_step_pulls_old_waits_forward_once(journey):
    _, queue_id, _ = add_page(journey, status="RETRY_WAIT")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type="DOCUMENT_INGEST", work_key=str(queue_id), payload={}, correlation_id=None)
    _sql(journey, "UPDATE auditcore.p2_work_queue SET work_status='RETRY_WAIT', attempt_count=1, "
                  "next_attempt_at_utc=now() + interval '2 hours' WHERE tenant_id=:t AND work_key=:k",
         k=str(queue_id))
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert worker.apply_retry_policy_rollout(connection, tenant_id=journey.tenant_id) == 1
        assert worker.apply_retry_policy_rollout(connection, tenant_id=journey.tenant_id) is None
    row = queue_row(journey, "DOCUMENT_INGEST", str(queue_id))
    assert row["next_attempt_at_utc"] <= datetime.now(UTC)
    assert _page(journey, queue_id)["status_reason"] == ("Retrying now (the automatic retries were shortened to "
                                                         "three attempts within 20 minutes). Nothing to do yet.")


def test_the_nightly_review_is_claimed_once_per_night_durably(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert claim_scheduled_run(connection, tenant_id=journey.tenant_id, run_name="P2_NIGHTLY_REVIEW",
                                   run_key="2026-10-20") is True
    with journey.engine.begin() as connection:  # a restarted worker
        set_tenant_context(connection, journey.tenant_id)
        assert claim_scheduled_run(connection, tenant_id=journey.tenant_id, run_name="P2_NIGHTLY_REVIEW",
                                   run_key="2026-10-20") is False
        assert claim_scheduled_run(connection, tenant_id=journey.tenant_id, run_name="P2_NIGHTLY_REVIEW",
                                   run_key="2026-10-21") is True


def test_a_restarted_worker_does_not_run_the_night_again(journey, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "queue_nightly_upload_sweep",
                        lambda connection, **kwargs: calls.append(kwargs) or {"retried": 0})
    monkeypatch.setattr(worker, "_fact_sweep", lambda engine, tenant_id: 0)
    monkeypatch.setattr(worker, "backfill_price_variance", lambda engine, tenant_id: 0)
    monkeypatch.setattr(worker, "_NIGHTLY_REVIEW_UTC", "00:00")
    for _ in range(2):
        worker._last_sweep_at.pop(journey.tenant_id, None)
        worker._last_nightly_review.pop(journey.tenant_id, None)  # as after a restart
        worker._maybe_sweep(journey.engine, journey.tenant_id)
    assert calls == [{"tenant_id": journey.tenant_id}]


# ------------------------------------------------------------------- CONFLICT adoption

def test_a_conflict_after_a_finalize_timeout_adopts_the_stored_document(journey, monkeypatch):
    class Storage:
        def get_object(self, key):
            return b"%PDF-1.4 page"

    _, queue_id, _ = add_page(journey, status="DI_UPLOAD_PREPARING")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET di_document_id=NULL WHERE queue_id=:q", q=queue_id)
    client_upload_id = _page(journey, queue_id)["client_upload_id"]
    stored = uuid4()
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: Storage())
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type="DOCUMENT_INGEST", work_key=str(queue_id), payload={}, correlation_id=None)
    _run_ingest(journey, monkeypatch, FakeClient(
        [{"documentId": str(stored), "clientUploadId": client_upload_id, "state": "CLASSIFYING"}],
        intents={"uploads": [], "failures": [{"errorCode": "CONFLICT"}]},
    ))
    page = _page(journey, queue_id)
    assert page["queue_status"] == "CLASSIFYING" and page["di_document_id"] == stored
    assert queue_row(journey, "DOCUMENT_INGEST", str(queue_id))["work_status"] == "COMPLETED"


def test_a_conflict_with_no_stored_document_is_still_refused(journey, monkeypatch):
    class Storage:
        def get_object(self, key):
            return b"%PDF-1.4 page"

    _, queue_id, _ = add_page(journey, status="DI_UPLOAD_PREPARING")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET di_document_id=NULL WHERE queue_id=:q", q=queue_id)
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: Storage())
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type="DOCUMENT_INGEST", work_key=str(queue_id), payload={}, correlation_id=None)
    _run_ingest(journey, monkeypatch, FakeClient([], intents={"uploads": [], "failures": [{"errorCode": "CONFLICT"}]}))
    assert _page(journey, queue_id)["queue_status"] == "FAILED"


# ------------------------------------------------------------------- no silent dead letters

def _dead(journey, work_type, key, payload=None):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, work_type=work_type,
                     work_key=key, payload=payload or {}, correlation_id=None)
    [item] = [i for i in worker._claim_for_tenant(journey.engine, journey.tenant_id, 10) if i.work_key == key]
    worker._fail(journey.engine, item, TypeError("bug"))  # a programming error: final at once
    assert queue_row(journey, work_type, key)["work_status"] == "DEAD_LETTER"


def test_a_dead_reconcile_tells_the_team_lead(journey):
    _dead(journey, "JOURNEY_RECONCILE", str(journey.journey_id))
    [task] = _open_tasks(journey)
    assert task["task_type"] == "TL_DOCUMENT_STUCK" and task["severity"] == "HIGH"
    assert task["title"] == "Background step stopped: checking the document service for this booking's pages"


def _grouping_batch(journey, *, created_minutes_ago=0, grouping_status="GROUPING"):
    """A two-page booking form, both pages identified and waiting to be put
    together (SUPPORTING: a booking form is only ever read whole)."""
    batch_id, pages = add_batch_pages(journey, [("booking_form", "SUPPORTING"), ("booking_form", "SUPPORTING")],
                                      grouping_status=grouping_status)
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET created_at_utc=now() - (:m * interval '1 minute') "
                  "WHERE batch_id=:b", b=batch_id, m=created_minutes_ago)
    return batch_id, pages


def _withdrawals(journey):
    return sorted(r["work_key"] for r in _sql(
        journey, "SELECT work_key FROM auditcore.p2_work_queue WHERE tenant_id=:t AND journey_id=:j "
                 "AND work_type='DI_DOCUMENT_WITHDRAW'"))


def test_a_dead_grouping_raises_the_resubmit_task(journey):
    """Review of #469: a stopped grouping fails the pages that waited for it
    and the file's DOCUMENT_RESUBMIT task names them -- no task of its own."""
    batch_id, pages = _grouping_batch(journey)
    _dead(journey, "BATCH_GROUP", str(batch_id))
    [status] = _sql(journey, "SELECT grouping_status FROM auditcore.p2_upload_batches WHERE batch_id=:b", b=batch_id)
    assert status["grouping_status"] == "FAILED"
    for page in pages:
        row = _page(journey, page["queue_id"])
        assert (row["queue_status"], row["last_error"]) == ("FAILED", "GROUPING_FAILED")
    [task] = _open_tasks(journey)
    assert task["task_type"] == "DOCUMENT_RESUBMIT" and task["title"] == "packet.pdf: 2 pages to resubmit"
    assert "could not be put together" in task["description"]
    assert _withdrawals(journey) == sorted(str(p["di_document_id"]) for p in pages)


def test_the_grouping_resubmit_task_closes_when_the_pages_are_uploaded_again(journey):
    batch_id, pages = _grouping_batch(journey)
    _dead(journey, "BATCH_GROUP", str(batch_id))
    assert len(_open_tasks(journey)) == 1
    for page in pages:  # the same scans arrive in a fresh upload (superseded)
        _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='CANCELLED', last_error='SUPERSEDED_PAGE' "
                      "WHERE queue_id=:q", q=page["queue_id"])
    _sync_tasks(journey)
    assert _open_tasks(journey) == []


def test_grouping_gets_a_bounded_grace_then_fails_with_a_resubmit_task(journey):
    """Review of #469: grouping starts only once every page is identified,
    so it (and the document it makes) may settle up to 40 minutes after the
    upload; then it fails and the PC resubmits."""
    batch_id, pages = _grouping_batch(journey, created_minutes_ago=35, grouping_status="PENDING")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, work_type="BATCH_GROUP",
                     work_key=str(batch_id), payload={}, delay_seconds=600, correlation_id=None)
    assert _watchdog(journey) == 0  # 35 minutes: inside the grace
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET created_at_utc=now() - interval '41 minutes' "
                  "WHERE batch_id=:b", b=batch_id)
    assert _watchdog(journey) == 2
    assert queue_row(journey, "BATCH_GROUP", str(batch_id))["work_status"] == "CANCELLED"
    assert {_page(journey, p["queue_id"])["last_error"] for p in pages} == {"GROUPING_FAILED"}
    assert [t["task_type"] for t in _open_tasks(journey)] == ["DOCUMENT_RESUBMIT"]


def test_a_document_made_by_grouping_settles_within_40_minutes_of_the_upload(journey):
    batch_id, _ = _grouping_batch(journey, created_minutes_ago=35, grouping_status="GROUPED")
    [group] = _sql(journey, "INSERT INTO auditcore.p2_document_queue (tenant_id, batch_id, journey_id, page_number, "
                            "page_numbers, page_sha256, page_object_key, client_upload_id, queue_status, unit_kind) "
                            "VALUES (:t, :b, :j, 1, ARRAY[1,2], 'g', 'k', 'p2g-y', 'QUEUED', 'GROUP') "
                            "RETURNING queue_id", b=batch_id)
    assert _watchdog(journey) == 0  # made at 35 minutes: not failed for the grouping starting late
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET created_at_utc=now() - interval '41 minutes' "
                  "WHERE batch_id=:b", b=batch_id)
    assert _watchdog(journey) == 1
    assert _page(journey, group["queue_id"])["last_error"] == "NOT_READ_IN_30_MIN"


# ------------------------------------------------------------------- the clock against running work

def test_a_page_failed_while_its_send_was_running_stays_failed_and_its_di_copy_is_withdrawn(journey, monkeypatch):
    """Review of #469: the ingest was sending the page when the 30-minute
    clock failed it. Neither its retry nor its success writes over FAILED,
    the resubmit task does not flap, and the DI document it made is
    withdrawn (deleted at DI)."""
    batch_id, queue_id, _ = add_page(journey, status="DI_UPLOAD_PREPARING")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET di_document_id=NULL WHERE queue_id=:q", q=queue_id)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type="DOCUMENT_INGEST", work_key=str(queue_id), payload={}, correlation_id=None)
    [running] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    _uploaded_minutes_ago(journey, batch_id, 31)
    assert _watchdog(journey) == 1
    [before] = _open_tasks(journey)
    # The send fails meanwhile: its retry wait never lands on the page.
    worker._fail(journey.engine, running, DiCaptureV2Error(status_code=503, detail="down"))
    assert _page(journey, queue_id)["queue_status"] == "FAILED"
    # ... or it succeeds: the page stays FAILED and DI's new copy is withdrawn.
    row = {"client_upload_id": _page(journey, queue_id)["client_upload_id"], "uploaded_by_actor_id": "pc"}
    stored = uuid4()
    worker._record_submitted_page(journey.engine, running, row, queue_id=queue_id, di_document_id=stored,
                                  filename="scan-page-001.pdf", content_type="application/pdf")
    page = _page(journey, queue_id)
    assert (page["queue_status"], page["last_error"], page["di_document_id"]) == ("FAILED", "NOT_READ_IN_30_MIN", None)
    [after] = _open_tasks(journey)
    assert (after["task_type"], after["title"], after["description"]) == (
        before["task_type"], before["title"], before["description"])
    assert _withdrawals(journey) == [str(stored)]
    # The withdrawal deletes it at DI.
    client = FakeClient()
    monkeypatch.setattr(worker, "_di_context_and_requirements", lambda engine, work: ("ctx", "tok", [], {}))
    monkeypatch.setattr(worker, "get_di_capture_v2_client", lambda: client)
    [item] = [i for i in worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
              if i.work_type == "DI_DOCUMENT_WITHDRAW"]
    worker.process_work(journey.engine, item)
    assert client.deleted == [str(stored)]


def test_the_clock_withdraws_the_di_copy_of_a_page_it_fails_but_not_a_setup_page(journey):
    batch_id, _, reading_doc = add_page(journey, page_number=1, status="EXTRACTING")
    _, setup, _ = add_page(journey, page_number=2, status="QUEUED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET batch_id=:b, last_error=:e WHERE queue_id=:q",
         b=batch_id, q=setup, e=SETUP)
    _uploaded_minutes_ago(journey, batch_id, 31)
    assert _watchdog(journey) == 2
    assert _withdrawals(journey) == [str(reading_doc)]  # DI re-reads the setup page at night


def test_the_background_step_task_closes_when_the_step_runs_again(journey):
    _dead(journey, "JOURNEY_RECONCILE", str(journey.journey_id))
    [task] = _open_tasks(journey)
    # The TL marks it done while the step is still stopped: it comes back.
    _sql(journey, "UPDATE auditcore.p2_tasks SET task_status='VERIFYING', "
                  "completion_result=jsonb_build_object('submittedAt', CAST(now() - interval '1 minute' AS text)) "
                  "WHERE tenant_id=:t AND dedupe_key=:k", k=task["dedupe_key"])
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker.sync_dead_work_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    [status] = _sql(journey, "SELECT task_status FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k",
                    k=task["dedupe_key"])
    assert status["task_status"] == "RETURNED"
    # Re-sync runs the reconcile again and it completes: the task closes itself.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type="JOURNEY_RECONCILE", work_key=str(journey.journey_id), payload={}, correlation_id=None)
    [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    worker._complete(journey.engine, item)
    assert _open_tasks(journey) == []


def test_a_poll_that_waits_resets_its_failure_count(journey):
    key = str(journey.journey_id)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type="JOURNEY_RECONCILE", work_key=key, payload={}, correlation_id=None)
    [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    worker._fail(journey.engine, item, worker.DiCaptureV2Error(status_code=504, detail="DI V2 request failed: x"))
    row = queue_row(journey, "JOURNEY_RECONCILE", key)
    assert row["attempt_count"] == 1 and row["work_status"] == "RETRY_WAIT"
    _due_now(journey, "JOURNEY_RECONCILE", key)
    [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    worker._reschedule(journey.engine, item, worker.RescheduleWork("wait", delay_seconds=30))
    assert queue_row(journey, "JOURNEY_RECONCILE", key)["attempt_count"] == 0
