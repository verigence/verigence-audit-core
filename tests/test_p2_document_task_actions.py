"""Document task actions (product owner 2026-10-27), real Postgres:

- re-typing a page starts its 30 minutes now (it was timed from the upload, so
  a page re-typed on an old file was failed by the next watchdog run), and is
  refused on a stage whose documents are marked reviewed unless a Team Lead
  overrides it with a reason;
- "Document to resubmit": a page the day-end sweep cleared is sent again from
  its stored file -- "Send again" (Retry) when its type is known, "Set type
  and send" when it is not -- and the swept copy is marked replaced, so the
  file's task closes;
- "if the task is closed, then automatic retries shouldn't happen, else keep
  it continued as normal" (uc03_p2_retry_stop).
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from fastapi import HTTPException
from p2_support import (
    AllowAllAuthorization,
    add_batch_pages,
    create_p2_journey,
    database_engine,
    principal,
    queue_row,
)
from sqlalchemy import text
from starlette.requests import Request

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.di_capture_v2_client import DiCaptureV2Error
from audit_core.uc03_p2_api import (
    ReReadPageCommand,
    SetPageTypeCommand,
    retry_page,
    set_page_type,
)
from audit_core.uc03_p2_page_removal import sweep_failed_pages
from audit_core.uc03_p2_retry_stop import STOPPED_CODE, automatic_retry_stop_reason
from audit_core.uc03_p2_runtime import enqueue_work, swept_pending_sql
from audit_core.uc03_p2_task_producer import (
    sync_processing_failure_tasks,
    sync_unclassified_page_tasks,
)
from audit_core.uc03_p2_tasks import submit_action

NOT_READ = ("NOT_READ_IN_30_MIN", "Not read within 30 minutes. Please upload this page again.")


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2dta")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _sql(journey, statement, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        result = connection.execute(text(statement), {"t": journey.tenant_id, "j": journey.journey_id, **params})
        return [dict(r) for r in result.mappings().all()] if result.returns_rows else None


def _unit(journey, queue_id):
    [row] = _sql(journey, "SELECT * FROM auditcore.p2_document_queue WHERE queue_id=:q", q=queue_id)
    return row


def _set_type(journey, queue_id, template, *, name=None, reason=None, actor=None, role="PC"):
    with journey.engine.begin() as connection:
        return set_page_type(
            tenant_id=journey.tenant_id, journey_id=journey.journey_id, queue_id=queue_id,
            command=SetPageTypeCommand(templateKey=template, name=name, reason=reason), request=_request(),
            human_principal=SimpleNamespace(subject=actor) if actor else principal(journey),
            authorization_client=AllowAllAuthorization(role_key=role), connection=connection,
        )


def _retry(journey, queue_id, *, reason=None, actor=None, role="PC"):
    with journey.engine.begin() as connection:
        return retry_page(
            tenant_id=journey.tenant_id, journey_id=journey.journey_id, queue_id=queue_id, request=_request(),
            human_principal=SimpleNamespace(subject=actor) if actor else principal(journey),
            authorization_client=AllowAllAuthorization(role_key=role), connection=connection,
            payload=ReReadPageCommand(reason=reason) if reason else None,
        )


def _team_lead(journey) -> str:
    actor = f"p2dta-tl-{uuid4().hex[:8]}"
    _sql(journey, "INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                  "dealer_id, outlet_id) VALUES (:t, :a, 'TL', :d, :o)",
         a=actor, d=journey.dealer_id, o=journey.outlet_id)
    return actor


def _mark_reviewed(journey, stage):
    _sql(journey, "INSERT INTO auditcore.p2_stage_document_reviews (tenant_id, journey_id, stage_code, "
                  "reviewed_by_actor_id, reviewed_by_role_code, document_count, dealer_id, outlet_id) "
                  "VALUES (:t, :j, :s, 'rev', 'REVIEWER', 1, :d, :o)",
         s=stage, d=journey.dealer_id, o=journey.outlet_id)


def _sync(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        sync_processing_failure_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        sync_unclassified_page_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _task(journey, prefix, batch_or_queue):
    rows = _sql(journey, "SELECT task_id, task_type, task_status, reference, completion_result "
                         "FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k",
                k=f"{prefix}:{journey.journey_id}:{batch_or_queue}")
    return rows[0] if rows else None


def _swept_file(journey, pages, *, template_keys=None):
    """A file whose pages failed on the 30-minute clock and were cleared by
    the day-end sweep; ``template_keys`` gives each page the type it was
    identified as (None: never identified)."""
    batch_id, rows = add_batch_pages(journey, pages, grouping_status="NOT_NEEDED")
    for row, key in zip(rows, template_keys or [None] * len(rows), strict=True):
        _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='FAILED', last_error=:e, "
                      "status_reason=:r, template_key=:k WHERE queue_id=:q",
             q=row["queue_id"], e=NOT_READ[0], r=NOT_READ[1], k=key)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        sweep_failed_pages(connection, tenant_id=journey.tenant_id)
    _sync(journey)
    return batch_id, rows


def _swept_pending(journey, queue_id) -> bool:
    return bool(_sql(journey, f"SELECT 1 FROM auditcore.p2_document_queue q WHERE q.queue_id=:q "
                              f"AND {swept_pending_sql('q')}", q=queue_id))


# ---------------------------------------------------------------- set-type: clock and reviewed stage

def test_retyping_starts_the_pages_thirty_minutes_now(journey):
    """The re-typed unit used to carry no read clock, so on a file uploaded
    more than 30 minutes earlier the next watchdog run failed it unread."""
    batch_id, [page] = add_batch_pages(journey, [(None, "SUPPORTING")], grouping_status="NOT_NEEDED")
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET created_at_utc=now() - interval '3 days' WHERE batch_id=:b",
         b=batch_id)
    result = _set_type(journey, page["queue_id"], "customer_kyc")
    new_unit = _unit(journey, UUID(result["queueId"]))
    assert new_unit["read_clock_started_at_utc"] is not None
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker.fail_unsettled_pages(connection, tenant_id=journey.tenant_id)
    assert _unit(journey, UUID(result["queueId"]))["queue_status"] == "QUEUED"


def test_retyping_into_a_reviewed_stage_is_refused_for_a_pc(journey):
    _, [page] = add_batch_pages(journey, [(None, "SUPPORTING")], grouping_status="NOT_NEEDED")
    _mark_reviewed(journey, "BOOKING")
    with pytest.raises(HTTPException) as refused:
        _set_type(journey, page["queue_id"], "customer_kyc")  # a Booking document
    assert refused.value.status_code == 409 and "marked reviewed" in refused.value.detail
    assert _unit(journey, page["queue_id"])["queue_status"] == "SUPPORTING"
    # Others is never read, so nothing reviewed changes: still allowed.
    assert _set_type(journey, page["queue_id"], "supporting_document", name="Letter")["status"] == "SUPPORTING"


def test_retyping_into_an_unreviewed_stage_is_allowed(journey):
    _, [page] = add_batch_pages(journey, [(None, "SUPPORTING")], grouping_status="NOT_NEEDED")
    _mark_reviewed(journey, "DELIVERY")
    assert _set_type(journey, page["queue_id"], "customer_kyc")["status"] == "QUEUED"


def test_a_team_lead_overrides_the_reviewed_stage_with_a_reason(journey):
    _, [page] = add_batch_pages(journey, [(None, "SUPPORTING")], grouping_status="NOT_NEEDED")
    _mark_reviewed(journey, "BOOKING")
    tl = _team_lead(journey)
    with pytest.raises(HTTPException) as unexplained:
        _set_type(journey, page["queue_id"], "customer_kyc", actor=tl, role="TL")
    assert unexplained.value.status_code == 422
    result = _set_type(journey, page["queue_id"], "customer_kyc", actor=tl, role="TL", reason="KYC page was missed")
    assert result["status"] == "QUEUED"
    [event] = _sql(journey, "SELECT details FROM auditcore.p2_activity_events WHERE tenant_id=:t "
                            "AND event_type='PAGE_RETYPED'")
    assert event["details"]["overrideReason"] == "KYC page was missed"


# ---------------------------------------------------------------- Document to resubmit: send again

def test_the_resubmit_task_says_which_pages_have_a_known_type(journey):
    batch_id, rows = _swept_file(journey, [("aadhaar", "QUEUED"), (None, "QUEUED")],
                                 template_keys=["aadhaar", None])
    task = _task(journey, "resubmit", batch_id)
    assert task["task_status"] == "READY"
    by_queue = {p["queueId"]: p for p in task["reference"]["pages"]}
    assert by_queue[str(rows[0]["queue_id"])]["templateKey"] == "aadhaar"
    assert by_queue[str(rows[1]["queue_id"])]["templateKey"] is None


def test_send_again_reads_a_swept_page_of_known_type_from_its_stored_file(journey):
    batch_id, [page] = _swept_file(journey, [("aadhaar", "QUEUED")], template_keys=["aadhaar"])
    old = _unit(journey, page["queue_id"])
    assert old["queue_status"] == "CANCELLED" and _swept_pending(journey, page["queue_id"])

    result = _retry(journey, page["queue_id"])

    assert result["status"] == "QUEUED" and result["templateKey"] == "aadhaar"
    new_unit = _unit(journey, UUID(result["queueId"]))
    assert new_unit["queue_status"] == "QUEUED" and new_unit["candidate_override"] == ["aadhaar"]
    assert new_unit["page_object_key"] == old["page_object_key"] and new_unit["read_clock_started_at_utc"] is not None
    assert queue_row(journey, "DOCUMENT_INGEST", result["queueId"]) is not None
    replaced = _unit(journey, page["queue_id"])
    assert replaced["queue_status"] == "CANCELLED" and replaced["last_error"] == "SUPERSEDED_PAGE"
    assert not _swept_pending(journey, page["queue_id"])
    assert _task(journey, "resubmit", batch_id)["task_status"] == "VERIFIED_COMPLETE"
    [event] = _sql(journey, "SELECT details FROM auditcore.p2_activity_events WHERE tenant_id=:t "
                            "AND event_type='PAGE_SENT_AGAIN'")
    assert event["details"]["previousCode"] == NOT_READ[0] and event["details"]["newQueueId"] == result["queueId"]


def test_send_again_of_a_swept_page_of_unknown_type_asks_for_the_type(journey):
    _, [page] = _swept_file(journey, [(None, "QUEUED")])
    with pytest.raises(HTTPException) as refused:
        _retry(journey, page["queue_id"])
    assert refused.value.status_code == 409 and "type is not known" in refused.value.detail
    assert _swept_pending(journey, page["queue_id"])


def test_set_type_and_send_on_a_swept_page_of_unknown_type(journey):
    batch_id, [page, other] = _swept_file(journey, [(None, "QUEUED"), (None, "QUEUED")])
    result = _set_type(journey, page["queue_id"], "customer_kyc")
    assert result["status"] == "QUEUED"
    new_unit = _unit(journey, UUID(result["queueId"]))
    assert new_unit["candidate_override"] == ["customer_kyc"] and new_unit["read_clock_started_at_utc"] is not None
    assert _unit(journey, page["queue_id"])["last_error"] == "SUPERSEDED_PAGE"
    assert not _swept_pending(journey, page["queue_id"])
    # The other swept page still waits: the file's task stays open, naming it alone.
    task = _task(journey, "resubmit", batch_id)
    assert task["task_status"] == "READY"
    assert [p["queueId"] for p in task["reference"]["pages"]] == [str(other["queue_id"])]


def test_a_swept_quality_refused_page_is_not_sent_again(journey):
    _, [page] = add_batch_pages(journey, [(None, "FAILED")], grouping_status="NOT_NEEDED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='DI_QUALITY_IMAGE_BLUR_SCORE' WHERE queue_id=:q",
         q=page["queue_id"])
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        sweep_failed_pages(connection, tenant_id=journey.tenant_id)
    for action in (lambda: _set_type(journey, page["queue_id"], "customer_kyc"), lambda: _retry(journey, page["queue_id"])):
        with pytest.raises(HTTPException) as refused:
            action()
        assert refused.value.status_code == 409 and "scan quality" in refused.value.detail


def test_a_cancelled_page_that_was_not_swept_cannot_be_sent_again(journey):
    _, [page] = add_batch_pages(journey, [("aadhaar", "CANCELLED")], grouping_status="NOT_NEEDED")
    with pytest.raises(HTTPException):
        _retry(journey, page["queue_id"])
    with pytest.raises(HTTPException):
        _set_type(journey, page["queue_id"], "aadhaar")


def test_send_again_into_a_reviewed_stage_is_refused_for_a_pc(journey):
    _, [page] = _swept_file(journey, [("aadhaar", "QUEUED")], template_keys=["aadhaar"])
    _mark_reviewed(journey, "BOOKING")
    with pytest.raises(HTTPException) as refused:
        _retry(journey, page["queue_id"])
    assert refused.value.status_code == 409
    assert _swept_pending(journey, page["queue_id"])


# ---------------------------------------------------------------- no automatic retry once the task is closed

class _FailingClient:
    """DI does not answer: a retryable technical failure."""

    def create_upload_intents(self, **_kwargs):
        raise DiCaptureV2Error(status_code=503, detail="DI V2 request failed: ReadTimeout")


def _quality_task_closed_by_pc(journey, page):
    """A quality-refused page, its task closed by the PC with a comment."""
    _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='FAILED', "
                  "last_error='DI_QUALITY_IMAGE_BLUR_SCORE' WHERE queue_id=:q", q=page["queue_id"])
    _sync(journey)
    [task] = _sql(journey, "SELECT task_id FROM auditcore.p2_tasks WHERE tenant_id=:t "
                           "AND task_type='PC_PAGE_QUALITY_REJECTED'")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        submit_action(connection, tenant_id=journey.tenant_id, task_id=task["task_id"], action="CLOSE_TASK",
                      actor_id=journey.actor_id, actor_role_code="PC", comment="Document Uploaded")
    return task["task_id"]


def _queue_ingest(journey, queue_id, *, page_status="QUEUED", attempts=0):
    """Work for the page that is in flight (as left by work queued before the
    PC acted), without a person's new request (the read clock stays)."""
    _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status=:s, di_document_id=NULL WHERE queue_id=:q",
         q=queue_id, s=page_status)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type="DOCUMENT_INGEST", work_key=str(queue_id), payload={"queueId": str(queue_id)},
                     correlation_id=None)
    _sql(journey, "UPDATE auditcore.p2_work_queue SET attempt_count=:a WHERE tenant_id=:t AND work_key=:k "
                  "AND work_type='DOCUMENT_INGEST'", k=str(queue_id), a=attempts)


def _claim(journey):
    items = [i for i in worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
             if i.work_type == "DOCUMENT_INGEST"]
    assert items
    return items[0]


def _stop_reason(journey, queue_id):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return automatic_retry_stop_reason(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                           queue_id=queue_id)


def test_no_automatic_retry_once_the_pc_closed_the_pages_task(journey, monkeypatch):
    _, [page] = add_batch_pages(journey, [(None, "FAILED")], grouping_status="NOT_NEEDED")
    _quality_task_closed_by_pc(journey, page)
    assert "was closed" in _stop_reason(journey, page["queue_id"])

    _queue_ingest(journey, page["queue_id"])
    monkeypatch.setattr(worker, "_di_context_and_requirements", lambda engine, work: ("ctx", "tok", [], {}))
    monkeypatch.setattr(worker, "get_di_capture_v2_client", lambda: _FailingClient())
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: SimpleNamespace(get_object=lambda key: b"%PDF"))
    item = _claim(journey)
    try:
        worker._ingest_document(journey.engine, item)
    except Exception as exc:  # noqa: BLE001 - the worker's own failure path
        worker._fail(journey.engine, item, exc)

    work = queue_row(journey, "DOCUMENT_INGEST", str(page["queue_id"]))
    assert work["work_status"] == "CANCELLED" and work["next_attempt_at_utc"] is None
    unit = _unit(journey, page["queue_id"])
    assert unit["queue_status"] == "FAILED" and unit["last_error"] == STOPPED_CODE
    assert "Not tried again automatically" in unit["status_reason"]
    assert _sql(journey, "SELECT 1 FROM auditcore.p2_activity_events WHERE tenant_id=:t "
                         "AND event_type='AUTOMATIC_RETRY_STOPPED' AND subject_id=:q", q=str(page["queue_id"]))


def test_retries_continue_as_normal_while_the_task_is_open(journey, monkeypatch):
    _, [page] = add_batch_pages(journey, [(None, "FAILED")], grouping_status="NOT_NEEDED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='DI_QUALITY_IMAGE_BLUR_SCORE' WHERE queue_id=:q",
         q=page["queue_id"])
    _sync(journey)  # the quality task is open
    assert _stop_reason(journey, page["queue_id"]) is None
    _queue_ingest(journey, page["queue_id"])
    monkeypatch.setattr(worker, "_di_context_and_requirements", lambda engine, work: ("ctx", "tok", [], {}))
    monkeypatch.setattr(worker, "get_di_capture_v2_client", lambda: _FailingClient())
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: SimpleNamespace(get_object=lambda key: b"%PDF"))
    item = _claim(journey)
    try:
        worker._ingest_document(journey.engine, item)
    except Exception as exc:  # noqa: BLE001
        worker._fail(journey.engine, item, exc)
    work = queue_row(journey, "DOCUMENT_INGEST", str(page["queue_id"]))
    assert work["work_status"] == "RETRY_WAIT" and work["next_attempt_at_utc"] is not None
    assert _unit(journey, page["queue_id"])["queue_status"] == "RETRY_WAIT"


def test_a_retry_already_scheduled_does_not_run_after_the_task_is_closed(journey, monkeypatch):
    _, [page] = add_batch_pages(journey, [(None, "FAILED")], grouping_status="NOT_NEEDED")
    _quality_task_closed_by_pc(journey, page)
    _queue_ingest(journey, page["queue_id"], page_status="RETRY_WAIT", attempts=1)
    ran: list[str] = []
    monkeypatch.setattr(worker, "_ingest_document", lambda engine, work: ran.append(work.work_key))
    item = _claim(journey)
    assert item.attempt_count == 1
    worker.process_work(journey.engine, item)
    worker._complete(journey.engine, item)
    assert ran == []
    assert _unit(journey, page["queue_id"])["queue_status"] == "FAILED"
    assert queue_row(journey, "DOCUMENT_INGEST", str(page["queue_id"]))["work_status"] == "COMPLETED"


def test_a_first_attempt_is_never_stopped(journey, monkeypatch):
    """Only automatic retries stop: the first attempt of queued work runs."""
    _, [page] = add_batch_pages(journey, [(None, "FAILED")], grouping_status="NOT_NEEDED")
    _quality_task_closed_by_pc(journey, page)
    _queue_ingest(journey, page["queue_id"])
    ran: list[str] = []
    monkeypatch.setattr(worker, "_ingest_document", lambda engine, work: ran.append(work.work_key))
    worker.process_work(journey.engine, _claim(journey))
    assert ran == [str(page["queue_id"])]


def test_a_persons_later_request_restarts_automatic_retries(journey):
    """Retry / Read again / Send again after the task was closed restart the
    page's clock: its retries are normal again."""
    _, [page] = add_batch_pages(journey, [(None, "FAILED")], grouping_status="NOT_NEEDED")
    _quality_task_closed_by_pc(journey, page)
    assert _stop_reason(journey, page["queue_id"]) is not None
    _sql(journey, "UPDATE auditcore.p2_document_queue SET read_clock_started_at_utc=now() + interval '1 second' "
                  "WHERE queue_id=:q", q=page["queue_id"])
    assert _stop_reason(journey, page["queue_id"]) is None


def test_a_task_the_system_closed_does_not_stop_retries(journey):
    """The resubmit task closing because the page was read or sent again is
    not the PC closing it: retries continue as normal."""
    batch_id, [page] = add_batch_pages(journey, [(None, "FAILED")], grouping_status="NOT_NEEDED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='DOCUMENT_AI_RESPONSE_INVALID' WHERE queue_id=:q",
         q=page["queue_id"])
    _sync(journey)
    assert _task(journey, "resubmit", batch_id)["task_status"] == "READY"
    _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='RETRY_WAIT' WHERE queue_id=:q",
         q=page["queue_id"])
    _sync(journey)
    assert _task(journey, "resubmit", batch_id)["task_status"] == "VERIFIED_COMPLETE"
    assert _stop_reason(journey, page["queue_id"]) is None


def test_a_page_kept_as_others_is_not_read_again_automatically(journey):
    _, [page] = add_batch_pages(journey, [(None, "SUPPORTING")], grouping_status="NOT_NEEDED")
    _sync(journey)
    _set_type(journey, page["queue_id"], "supporting_document", name="Employer letter")
    assert _stop_reason(journey, page["queue_id"]) == "it was kept as Others"


def test_the_resend_job_skips_pages_whose_task_the_pc_closed(journey):
    from audit_core.uc03_p2_page_resend import _candidates

    _, [closed, open_] = add_batch_pages(journey, [(None, "FAILED"), (None, "FAILED")],
                                         grouping_status="NOT_NEEDED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='DOCUMENT_AI_RESPONSE_INVALID' "
                  "WHERE queue_id=:q", q=open_["queue_id"])
    _sync(journey)
    # The PC closes the first page's (quality) task; its code is then a technical one.
    _quality_task_closed_by_pc(journey, closed)
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='DOCUMENT_AI_RESPONSE_INVALID' "
                  "WHERE queue_id=:q", q=closed["queue_id"])
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        picked = {str(r["queue_id"]) for r in _candidates(connection, journey.tenant_id)}
    assert str(open_["queue_id"]) in picked and str(closed["queue_id"]) not in picked
