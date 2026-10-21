"""Retry policy review fixes (2026-10-20, review of PR #469): a queued night
attempt is never told as failed, the nightly look-up survives a DI hiccup,
night-waiting pages have a morning deadline, Retry cannot stack on the night
attempt, the one-time deploy step, durable nightly claims, CONFLICT adoption,
no silent dead letters, and refused AI credentials go to the Team Lead
(real Postgres)."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import add_page, create_p2_journey, database_engine, queue_row
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_document_health import night_attempt_deadline
from audit_core.uc03_p2_runtime import claim_scheduled_run, enqueue_work

MARK = "AI_RETRY_TONIGHT:DOCUMENT_AI_UNAVAILABLE"
_AI_FAILED = {"state": "CLASSIFIED", "processingStatus": "FAILED", "processingFailureCode": "DOCUMENT_AI_UNAVAILABLE",
              "failureKind": "AI", "scanPassed": True}


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

    def list_documents(self, *, token, tenant_id, external_context_ref, phase):
        if self.fail_listing:
            raise worker.DiCaptureV2Error(status_code=504, detail="DI V2 request failed: ReadTimeout")
        return {"documents": self.documents if phase == "BOOKING" else []}

    def create_upload_intents(self, **_kwargs):
        return self.intents


def _sql(journey, statement, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        result = connection.execute(text(statement), {"t": journey.tenant_id, "j": journey.journey_id, **params})
        return result.mappings().all() if result.returns_rows else None


def _page(journey, queue_id):
    [row] = _sql(journey, "SELECT queue_status, status_reason, last_error, di_document_id, client_upload_id "
                          "FROM auditcore.p2_document_queue WHERE queue_id=:q", q=queue_id)
    return row


def _open_tasks(journey):
    return _sql(journey, "SELECT task_type, severity, title, description, dedupe_key FROM auditcore.p2_tasks "
                         "WHERE tenant_id=:t AND journey_id=:j AND task_status='READY' ORDER BY dedupe_key")


def _overnight_page(journey):
    batch_id, queue_id, document_id = add_page(journey, status="FAILED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error=:e, status_reason='waiting overnight' "
                  "WHERE queue_id=:q", q=queue_id, e=MARK)
    return batch_id, queue_id, document_id


def _sweep(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return worker.queue_nightly_upload_sweep(connection, tenant_id=journey.tenant_id)


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


# ------------------------------------------------------------------- B1

@pytest.mark.parametrize("marker", ["n", "r"])
def test_a_queued_night_attempt_is_polled_never_told_as_failed(marker):
    """DI has queued the night attempt but not run it: the document still
    says FAILED, and DI reports the attempt it holds."""
    item = {**_AI_FAILED, "nextAttemptAtUtc": "2026-10-20T17:00:00+00:00", "inNightAttempt": True,
            "retryTonight": True}
    now = datetime(2026, 10, 20, 20, 30, tzinfo=UTC)
    assert worker.recovery_decision(di_item=item, marker=marker, submitted_at=now, now=now) == "EXTRACTING"


def test_the_sweep_meeting_a_queued_night_attempt_polls_and_tells_no_one(journey, monkeypatch):
    _, queue_id, document_id = _overnight_page(journey)
    _sweep(journey)
    _run_ingest(journey, monkeypatch, FakeClient([{**_AI_FAILED, "documentId": str(document_id),
                                                   "nextAttemptAtUtc": "2026-10-20T17:00:00+00:00",
                                                   "inNightAttempt": True, "retryTonight": True}]))
    page = _page(journey, queue_id)
    assert page["queue_status"] == "EXTRACTING" and page["last_error"] is None
    assert page["status_reason"] == "AI reading failed earlier — the overnight attempt is under way. Nothing to do now."
    assert queue_row(journey, "JOURNEY_RECONCILE", str(journey.journey_id))["work_status"] == "PENDING"
    assert _open_tasks(journey) == []


# ------------------------------------------------------------------- S2

def test_a_di_hiccup_during_the_nightly_lookup_keeps_the_night_marker(journey, monkeypatch):
    _, queue_id, _ = _overnight_page(journey)
    _sweep(journey)
    assert _page(journey, queue_id)["last_error"] == MARK  # kept while the look-up is queued
    _run_ingest(journey, monkeypatch, FakeClient(fail_listing=True))
    row = queue_row(journey, "DOCUMENT_INGEST", str(queue_id))
    assert row["work_status"] == "RETRY_WAIT" and row["attempt_count"] == 1
    page = _page(journey, queue_id)
    assert page["queue_status"] == "QUEUED" and page["last_error"] == MARK
    assert page["status_reason"] == "waiting overnight"  # untouched: no "trying once more" for a look-up
    assert _open_tasks(journey) == []

    # It keeps failing for an hour: the page goes back to waiting, marker kept, PC not told.
    _sql(journey, "UPDATE auditcore.p2_work_queue SET attempt_count=12, next_attempt_at_utc=now() "
                  "WHERE tenant_id=:t AND work_type='DOCUMENT_INGEST' AND work_key=:k", k=str(queue_id))
    _run_ingest(journey, monkeypatch, FakeClient(fail_listing=True))
    assert queue_row(journey, "DOCUMENT_INGEST", str(queue_id))["work_status"] == "DEAD_LETTER"
    page = _page(journey, queue_id)
    assert page["queue_status"] == "FAILED" and page["last_error"] == MARK
    assert "could not reach the document service" in page["status_reason"]
    assert _open_tasks(journey) == []
    # ... and the next sweep looks again.
    assert _sweep(journey)["retried"] == 1


# ------------------------------------------------------------------- S3

def test_the_night_attempt_deadline_is_8_am_ist_the_morning_after():
    # failed in the afternoon (IST): night run 22:30 IST the same day, told 08:00 IST next morning
    assert night_attempt_deadline(datetime(2026, 10, 20, 9, 0, tzinfo=UTC)) == datetime(2026, 10, 21, 2, 30, tzinfo=UTC)
    # submitted at 22:15 IST: its retries end after 22:30 IST, so it waits for the next night
    assert night_attempt_deadline(datetime(2026, 10, 20, 16, 45, tzinfo=UTC)) == datetime(2026, 10, 22, 2, 30,
                                                                                         tzinfo=UTC)


def test_a_page_still_waiting_the_morning_after_is_told_to_the_pc(journey):
    _, queue_id, _ = _overnight_page(journey)
    _sql(journey, "UPDATE auditcore.p2_document_queue SET di_submitted_at_utc=:s WHERE queue_id=:q",
         q=queue_id, s=datetime(2026, 10, 20, 9, 0, tzinfo=UTC))
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert worker.expire_night_waits(connection, tenant_id=journey.tenant_id,
                                         now=datetime(2026, 10, 21, 2, 0, tzinfo=UTC)) == 0
    assert _page(journey, queue_id)["last_error"] == MARK
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert worker.expire_night_waits(connection, tenant_id=journey.tenant_id,
                                         now=datetime(2026, 10, 21, 2, 31, tzinfo=UTC)) == 1
    page = _page(journey, queue_id)
    assert page["last_error"] == "DOCUMENT_AI_UNAVAILABLE"
    assert page["status_reason"].startswith("AI reading still had not succeeded by 08:00 the morning after")
    [task] = _open_tasks(journey)
    assert task["task_type"] == "PC_UPLOAD_STATUS" and task["severity"] == "HIGH"


# ------------------------------------------------------------------- S4

def test_retry_is_refused_for_a_page_waiting_for_the_night_attempt(journey):
    from p2_support import AllowAllAuthorization

    from audit_core.dependencies import get_human_principal
    from audit_core.main import app
    from audit_core.security import HumanPrincipal
    from audit_core.security_authorization import get_security_authorization_client

    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        _, queue_id, _ = _overnight_page(journey)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/pages/{queue_id}:retry")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 409, response.text
    assert "waiting for its overnight AI retry" in response.json()["detail"]
    assert _page(journey, queue_id)["queue_status"] == "FAILED"


# ------------------------------------------------------------------- S5 and the durable nightly claim

def test_the_one_time_deploy_step_pulls_old_waits_forward_once(journey):
    _, queue_id, _ = add_page(journey, status="RETRY_WAIT")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type="DOCUMENT_INGEST", work_key=str(queue_id), payload={}, correlation_id=None)
    _sql(journey, "UPDATE auditcore.p2_work_queue SET work_status='RETRY_WAIT', attempt_count=3, "
                  "next_attempt_at_utc=now() + interval '2 hours' WHERE tenant_id=:t AND work_key=:k",
         k=str(queue_id))
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert worker.apply_retry_policy_rollout(connection, tenant_id=journey.tenant_id) == 1
        assert worker.apply_retry_policy_rollout(connection, tenant_id=journey.tenant_id) is None
    row = queue_row(journey, "DOCUMENT_INGEST", str(queue_id))
    assert row["next_attempt_at_utc"] <= datetime.now(UTC)
    assert _page(journey, queue_id)["status_reason"].startswith("Retrying now (the automatic retries were shortened")


def test_the_first_sweep_also_looks_up_recent_ai_failures_from_before_the_deploy(journey):
    _, old_ai, _ = add_page(journey, page_number=1, status="FAILED")
    _, quality, _ = add_page(journey, page_number=2, status="FAILED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='DOCUMENT_AI_UNAVAILABLE' WHERE queue_id=:q",
         q=old_ai)
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='DI_QUALITY_IMAGE_BLUR_SCORE' "
                  "WHERE queue_id=:q", q=quality)
    assert _sweep(journey)["retried"] == 0
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        swept = worker.queue_nightly_upload_sweep(connection, tenant_id=journey.tenant_id, include_recent_failed=True)
    assert swept["retried"] == 1
    assert _page(journey, old_ai)["queue_status"] == "QUEUED"
    assert _page(journey, quality)["queue_status"] == "FAILED"


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
    assert len(calls) == 1
    assert calls[0]["include_recent_failed"] is True  # the first sweep after the deploy


# ------------------------------------------------------------------- S7

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


# ------------------------------------------------------------------- S1: no silent dead letters

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


def test_a_dead_read_again_fails_the_page_for_the_pc(journey):
    _, queue_id, _ = add_page(journey, status="NEEDS_REVIEW")
    _dead(journey, "PAGE_REREAD", str(queue_id))
    page = _page(journey, queue_id)
    assert page["queue_status"] == "FAILED" and '"Read again" could not be sent' in page["status_reason"]
    assert [t["task_type"] for t in _open_tasks(journey)] == ["PC_UPLOAD_STATUS"]


def test_a_dead_grouping_tells_the_pc(journey):
    batch_id, _, _ = add_page(journey, status="CLASSIFYING")
    _dead(journey, "BATCH_GROUP", str(batch_id))
    [status] = _sql(journey, "SELECT grouping_status FROM auditcore.p2_upload_batches WHERE batch_id=:b", b=batch_id)
    assert status["grouping_status"] == "FAILED"
    [task] = _open_tasks(journey)
    assert task["task_type"] == "PC_RESOLVE_DOCUMENT_PROCESSING_FAILURE"
    assert "could not be put together" in task["title"]


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


# ------------------------------------------------------------------- S8 and N5

def test_refused_ai_credentials_raise_a_team_lead_alert_not_a_pc_task(journey):
    _, queue_id, _ = add_page(journey, status="FAILED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='AI_RETRY_TONIGHT:DOCUMENT_AI_NOT_CONFIGURED' "
                  "WHERE queue_id=:q", q=queue_id)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker.sync_processing_failure_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    [task] = _open_tasks(journey)
    assert task["task_type"] == "TL_DOCUMENT_STUCK" and "refused our credentials" in task["title"]


def test_a_page_waiting_for_the_night_does_not_make_the_booking_stuck(journey):
    from audit_core.uc03_p2_workflow import cancel_stuck_journey

    _overnight_page(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert cancel_stuck_journey(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                    actor_id=journey.actor_id, actor_role="PC") == "NOT_STUCK"

