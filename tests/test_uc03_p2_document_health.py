"""Decision 2026-10-01: a page that passed quality and was classified must,
within eight hours, hold values in Audit Core or carry a reason and an
action a person can take. These tests cover the health state per page, the
"Read again" path (PAGE_REREAD work -> DI reprocess, never a re-upload),
the nightly re-read, the 8h / 24h file status tasks, the page actions and
the restore of a superseded copy."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from fastapi import HTTPException
from p2_support import (
    AllowAllAuthorization,
    add_evidence,
    add_page,
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
from audit_core.uc03_p2_api import reread_page, restore_document_copy, resync_page
from audit_core.uc03_p2_document_health import (
    DEFECT_STATES,
    STUCK_AFTER_SECONDS,
    journey_document_health,
    page_health,
    summarize,
)
from audit_core.uc03_p2_runtime import enqueue_work, request_page_reread
from audit_core.uc03_p2_task_producer import sync_processing_failure_tasks

_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2dh")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _unit(hours_old: float = 1.0, **fields):
    base = {
        "queue_status": "EXTRACTING", "template_key": "booking_docket", "type_overridden_by_actor_id": None,
        "last_error": None, "di_submitted_at_utc": _NOW - timedelta(hours=hours_old), "created_at_utc": None,
        "di_document_id": uuid4(),
    }
    base.update(fields)
    return base


# ------------------------------------------------------------ one page's state

@pytest.mark.parametrize(
    ("unit", "capture_status", "state", "action"),
    [
        (_unit(queue_status="READY"), "CLASSIFIED", "READ", None),
        (_unit(queue_status="MERGED"), "CLASSIFIED", "HIDDEN", None),
        (_unit(queue_status="CANCELLED"), "CLASSIFIED", "HIDDEN", None),
        (_unit(queue_status="READY"), "SUPERSEDED", "SUPERSEDED", "RESTORE"),
        (_unit(queue_status="FAILED", last_error="DI_QUALITY_BLUR"), "FAILED", "REJECTED", "UPLOAD_AGAIN"),
        (_unit(queue_status="DEAD_LETTER", last_error="FILE_EMPTY"), None, "REJECTED", "UPLOAD_AGAIN"),
        (_unit(queue_status="DEAD_LETTER", last_error="boom"), None, "FAILED", "RETRY"),
        (_unit(queue_status="NEEDS_REVIEW"), "CLASSIFIED", "NOTHING_READ", "READ_AGAIN"),
        (_unit(queue_status="SUPPORTING", template_key=None), "UNKNOWN", "UNCLASSIFIED", "SET_TYPE"),
        (_unit(queue_status="SUPPORTING", template_key="supporting_document",
               type_overridden_by_actor_id="pc-1"), "UNKNOWN", "OTHERS", None),
        (_unit(queue_status="SUPPORTING", template_key="booking_docket"), "CLASSIFIED", "NOT_READ", "READ_AGAIN"),
        (_unit(hours_old=1), "CLASSIFIED", "WAITING", None),
        (_unit(hours_old=7.9), "CLASSIFIED", "WAITING", None),
        (_unit(hours_old=8), "CLASSIFIED", "STUCK", "READ_AGAIN"),
        (_unit(hours_old=9, queue_status="CLASSIFYING"), "CLASSIFYING", "STUCK", "READ_AGAIN"),
        (_unit(hours_old=9, queue_status="DI_UPLOADING", di_document_id=None), None, "STUCK", "RETRY"),
        (_unit(hours_old=1, queue_status="RETRY_WAIT"), "CLASSIFIED", "WAITING", None),
    ],
)
def test_page_health_states(unit, capture_status, state, action):
    health = page_health(unit, capture_status=capture_status, now=_NOW)
    assert (health["state"], health["action"]) == (state, action)
    assert health["ageSeconds"] >= 0


def test_page_health_uses_created_at_when_never_submitted():
    unit = _unit(hours_old=1, queue_status="QUEUED", di_submitted_at_utc=None,
                 created_at_utc=_NOW - timedelta(hours=9), di_document_id=None)
    health = page_health(unit, capture_status=None, now=_NOW)
    assert health["state"] == "STUCK" and health["action"] == "RETRY"
    assert health["ageSeconds"] == 9 * 3600


def test_summary_counts_states_and_defects():
    healths = [
        page_health(_unit(queue_status="READY"), capture_status="CLASSIFIED", now=_NOW),
        page_health(_unit(hours_old=9), capture_status="CLASSIFIED", now=_NOW),
        page_health(_unit(queue_status="SUPPORTING"), capture_status="CLASSIFIED", now=_NOW),
        page_health(_unit(queue_status="SUPPORTING", template_key=None), capture_status="UNKNOWN", now=_NOW),
        page_health(_unit(queue_status="MERGED"), capture_status="CLASSIFIED", now=_NOW),
    ]
    summary = summarize(healths)
    assert summary["read"] == 1 and summary["stuck"] == 1 and summary["notRead"] == 1
    assert summary["unclassified"] == 1 and summary["defects"] == 2
    assert DEFECT_STATES == {"STUCK", "NOT_READ"}


# --------------------------------------------------------- the journey's view

def _set_template(journey, queue_id, template_key, *, overridden_by=None):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET template_key=:k, type_overridden_by_actor_id=:o "
                 "WHERE tenant_id=:t AND queue_id=:q"),
            {"k": template_key, "o": overridden_by, "t": journey.tenant_id, "q": queue_id},
        )


def _age_page(journey, queue_id, *, hours: float):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET updated_at_utc=now() - (:h * interval '1 hour') "
                 "WHERE tenant_id=:t AND queue_id=:q"),
            {"h": hours, "t": journey.tenant_id, "q": queue_id},
        )


def _page_status(journey, queue_id):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT queue_status FROM auditcore.p2_document_queue WHERE queue_id=:q"), {"q": queue_id},
        ).scalar_one()


def _activities(journey, event_type):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [dict(r) for r in connection.execute(
            text("SELECT subject_id, details FROM auditcore.p2_activity_events "
                 "WHERE tenant_id=:t AND journey_id=:j AND event_type=:e"),
            {"t": journey.tenant_id, "j": journey.journey_id, "e": event_type},
        ).mappings().all()]


def test_journey_health_names_the_pages_the_pipeline_owes(journey):
    _, ready_q, _ = add_page(journey, page_number=1, status="READY")
    _, stuck_q, _ = add_page(journey, page_number=2, status="EXTRACTING", submitted_minutes_ago=9 * 60)
    _, waiting_q, _ = add_page(journey, page_number=3, status="EXTRACTING", submitted_minutes_ago=30)
    _, cancelled_q, _ = add_page(journey, page_number=4, status="CANCELLED")
    _, unread_q, _ = add_page(journey, page_number=5, status="SUPPORTING")
    _set_template(journey, stuck_q, "booking_docket")
    _set_template(journey, unread_q, "pan_card")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        health = journey_document_health(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert set(health["units"]) == {str(ready_q), str(stuck_q), str(waiting_q), str(unread_q)}
    assert str(cancelled_q) not in health["units"]
    assert health["units"][str(ready_q)]["state"] == "READ"
    assert health["units"][str(waiting_q)]["state"] == "WAITING"
    assert health["summary"]["defects"] == 2
    defects = {d["queueId"]: d for d in health["defects"]}
    assert defects[str(stuck_q)]["state"] == "STUCK" and defects[str(stuck_q)]["pageNumbers"] == [2]
    assert defects[str(stuck_q)]["filename"] == "scan.pdf" and defects[str(stuck_q)]["templateKey"] == "booking_docket"
    assert defects[str(unread_q)]["state"] == "NOT_READ"


# ---------------------------------------------------------- asking DI again

class FakeReprocessClient:
    def __init__(self, outcome: dict | Exception):
        self.outcome = outcome
        self.calls: list[dict] = []

    def reprocess_document(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _run_reread(journey, monkeypatch, queue_id, outcome):
    client = FakeReprocessClient(outcome)
    monkeypatch.setattr(worker, "_di_context_and_requirements", lambda engine, work: ("ctx", "tok", [], {}))
    monkeypatch.setattr(worker, "get_di_capture_v2_client", lambda: client)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        request_page_reread(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                            queue_id=queue_id, requested_by="pc-1", correlation_id="corr-1")
    [item] = [i for i in worker._claim_for_tenant(journey.engine, journey.tenant_id, 10) if i.work_type == "PAGE_REREAD"]
    assert item.work_key == str(queue_id)
    try:
        worker._page_reread(journey.engine, item)
    except Exception:
        worker._fail(journey.engine, item, RuntimeError("DI unavailable"))
        raise
    worker._complete(journey.engine, item)
    return client


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        ({"outcome": "queued", "processingJobId": "job-1"}, "EXTRACTING"),
        ({"outcome": "in_progress"}, "EXTRACTING"),
        ({"outcome": "already_processed"}, "SYNCING_TO_AUDIT_CORE"),
        ({"outcome": "not_classified", "captureState": "STORED"}, "CLASSIFYING"),
    ],
)
def test_read_again_asks_di_for_one_more_reading_and_follows_its_answer(journey, monkeypatch, outcome, expected_status):
    _, queue_id, di_document_id = add_page(journey, status="EXTRACTING", submitted_minutes_ago=9 * 60)
    client = _run_reread(journey, monkeypatch, queue_id, outcome)
    assert client.calls == [{"token": "tok", "tenant_id": journey.tenant_id, "external_context_ref": "ctx",
                             "document_id": str(di_document_id)}]
    assert _page_status(journey, queue_id) == expected_status
    assert queue_row(journey, "DOCUMENT_INGEST", str(queue_id)) is None  # never uploaded again
    assert queue_row(journey, "JOURNEY_RECONCILE", str(journey.journey_id))["work_status"] == "PENDING"
    [requested] = _activities(journey, "PAGE_REREAD_REQUESTED")
    assert requested["subject_id"] == str(queue_id) and requested["details"] == {"by": "pc-1"}
    [done] = _activities(journey, "PAGE_REREAD")
    assert done["details"]["diOutcome"] == outcome["outcome"] and done["details"]["status"] == expected_status
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        submitted = connection.execute(
            text("SELECT di_submitted_at_utc FROM auditcore.p2_document_queue WHERE queue_id=:q"), {"q": queue_id},
        ).scalar_one()
    assert datetime.now(UTC) - submitted < timedelta(minutes=5)  # the 8h clock restarts


def test_read_again_of_a_page_already_read_is_skipped_without_calling_di(journey, monkeypatch):
    _, queue_id, _ = add_page(journey, status="READY")
    client = FakeReprocessClient({"outcome": "queued"})
    monkeypatch.setattr(worker, "_di_context_and_requirements", lambda engine, work: ("ctx", "tok", [], {}))
    monkeypatch.setattr(worker, "get_di_capture_v2_client", lambda: client)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        enqueue_work(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                     work_type="PAGE_REREAD", work_key=str(queue_id), payload={}, correlation_id=None)
    [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    worker._page_reread(journey.engine, item)
    assert client.calls == [] and _page_status(journey, queue_id) == "READY"
    assert queue_row(journey, "JOURNEY_RECONCILE", str(journey.journey_id)) is None


def test_read_again_failure_at_di_leaves_the_page_as_it_was_and_retries_the_work(journey, monkeypatch):
    _, queue_id, _ = add_page(journey, status="EXTRACTING", submitted_minutes_ago=9 * 60)
    with pytest.raises(DiCaptureV2Error):
        _run_reread(journey, monkeypatch, queue_id, DiCaptureV2Error(status_code=503, detail="down"))
    assert _page_status(journey, queue_id) == "EXTRACTING"
    work = queue_row(journey, "PAGE_REREAD", str(queue_id))
    assert work["work_status"] == "RETRY_WAIT" and work["attempt_count"] == 1
    assert _activities(journey, "PAGE_REREAD") == []


def test_read_again_of_a_page_di_never_received_goes_through_recovery(journey):
    _, queue_id, _ = add_page(journey, status="DI_UPLOADING", submitted_minutes_ago=9 * 60)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_document_queue SET di_document_id=NULL WHERE queue_id=:q"),
                           {"q": queue_id})
        request_page_reread(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                            queue_id=queue_id, requested_by="pc-1", correlation_id=None)
    assert queue_row(journey, "PAGE_REREAD", str(queue_id)) is None
    assert queue_row(journey, "DOCUMENT_INGEST", str(queue_id))["work_status"] == "PENDING"
    assert _page_status(journey, queue_id) == "QUEUED"


# -------------------------------------------------------------- nightly sweep

def test_nightly_sweep_asks_again_for_every_classified_page_held_or_settled_unread(journey):
    _, stuck_q, _ = add_page(journey, page_number=1, status="CLASSIFYING", submitted_minutes_ago=9 * 60)
    _, fresh_q, _ = add_page(journey, page_number=2, status="EXTRACTING", submitted_minutes_ago=60)
    _, untyped_q, _ = add_page(journey, page_number=3, status="EXTRACTING", submitted_minutes_ago=9 * 60)
    _, unread_q, _ = add_page(journey, page_number=4, status="SUPPORTING")
    _, others_q, _ = add_page(journey, page_number=5, status="SUPPORTING")
    _, retyped_q, _ = add_page(journey, page_number=6, status="SUPPORTING")
    _, ready_q, _ = add_page(journey, page_number=7, status="READY")
    _set_template(journey, stuck_q, "booking_docket")
    _set_template(journey, fresh_q, "booking_docket")
    _set_template(journey, unread_q, "pan_card")
    _set_template(journey, others_q, "supporting_document", overridden_by="pc-1")
    _set_template(journey, retyped_q, "pan_card", overridden_by="pc-1")
    _set_template(journey, ready_q, "booking_docket")
    for queue_id in (unread_q, others_q, retyped_q, ready_q):
        _age_page(journey, queue_id, hours=9)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        swept = worker.queue_nightly_upload_sweep(connection, tenant_id=journey.tenant_id)
    assert swept["reread"] == 2 and swept["journeys"] == 1
    assert queue_row(journey, "PAGE_REREAD", str(stuck_q))["work_status"] == "PENDING"
    assert queue_row(journey, "PAGE_REREAD", str(unread_q))["work_status"] == "PENDING"
    for queue_id in (fresh_q, untyped_q, others_q, retyped_q, ready_q):
        assert queue_row(journey, "PAGE_REREAD", str(queue_id)) is None, queue_id
    # Asking twice (the sweep ran again) coalesces on the same pending work.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        swept = worker.queue_nightly_upload_sweep(connection, tenant_id=journey.tenant_id)
    assert swept["reread"] == 2
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        pending = connection.execute(
            text("SELECT COUNT(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND work_type='PAGE_REREAD'"),
            {"t": journey.tenant_id},
        ).scalar_one()
    assert pending == 2


# ---------------------------------------------------- the file's status tasks

def _age_batch(journey, batch_id, *, hours: float):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_upload_batches SET created_at_utc=now() - (:h * interval '1 hour') "
                 "WHERE tenant_id=:t AND batch_id=:b"),
            {"h": hours, "t": journey.tenant_id, "b": batch_id},
        )


def _tasks(journey, prefix):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [dict(r) for r in connection.execute(
            text("SELECT task_type, severity, task_status, title, description, reference FROM auditcore.p2_tasks "
                 "WHERE tenant_id=:t AND journey_id=:j AND dedupe_key LIKE :p ORDER BY created_at_utc"),
            {"t": journey.tenant_id, "j": journey.journey_id, "p": f"{prefix}%"},
        ).mappings().all()]


def test_file_status_task_tells_the_truth_after_eight_hours_and_the_tl_after_a_day(journey):
    batch_id, queue_id, _ = add_page(journey, status="EXTRACTING", submitted_minutes_ago=9 * 60)
    _age_batch(journey, batch_id, hours=9)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert sync_processing_failure_tasks(connection, tenant_id=journey.tenant_id,
                                             journey_id=journey.journey_id) == {"RAISED": 1}
    [pc_task] = _tasks(journey, "upload-status:")
    assert pc_task["task_type"] == "PC_UPLOAD_STATUS" and pc_task["severity"] == "INFO"
    assert "Waiting for the document service since" in pc_task["description"]
    assert "retried automatically tonight" in pc_task["description"]
    assert "Re-sync on Upload / Edit Documents" in pc_task["description"]
    assert "check back after 1 hour" not in pc_task["description"]
    assert _tasks(journey, "upload-stuck-tl:") == []

    _age_batch(journey, batch_id, hours=25)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        outcome = sync_processing_failure_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert outcome.get("RAISED") == 1
    [tl_task] = _tasks(journey, "upload-stuck-tl:")
    assert tl_task["task_type"] == "TL_DOCUMENT_STUCK" and tl_task["severity"] == "HIGH"
    assert tl_task["task_status"] == "READY"
    assert tl_task["title"] == "scan.pdf: not read for more than a day (0 of 1 pages read)"
    assert tl_task["reference"]["sourceCode"] == "DOCUMENT_PROCESSING_STUCK"
    assert tl_task["reference"]["pages"] == {"total": 1, "read": 0, "working": 1, "retrying": 0}

    # The page is read: both tasks close themselves.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_document_queue SET queue_status='READY' WHERE queue_id=:q"),
                           {"q": queue_id})
        sync_processing_failure_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert {t["task_status"] for t in _tasks(journey, "upload-status:")} == {"VERIFIED_COMPLETE"}
    assert {t["task_status"] for t in _tasks(journey, "upload-stuck-tl:")} == {"VERIFIED_COMPLETE"}


# ---------------------------------------------------------- the page actions

def _call(endpoint, journey, **kwargs):
    with journey.engine.begin() as connection:
        return endpoint(tenant_id=journey.tenant_id, journey_id=journey.journey_id, request=_request(),
                        human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
                        connection=connection, **kwargs)


def test_read_again_action_queues_the_work_and_refuses_a_read_page(journey):
    _, stuck_q, _ = add_page(journey, page_number=1, status="EXTRACTING", submitted_minutes_ago=9 * 60)
    _, ready_q, _ = add_page(journey, page_number=2, status="READY")
    result = _call(reread_page, journey, queue_id=stuck_q)
    assert result == {"queueId": str(stuck_q), "status": "REREAD_REQUESTED"}
    assert queue_row(journey, "PAGE_REREAD", str(stuck_q))["work_status"] == "PENDING"
    with pytest.raises(HTTPException) as refused:
        _call(reread_page, journey, queue_id=ready_q)
    assert refused.value.status_code == 409


def test_sync_action_copies_again_without_a_new_reading(journey):
    _, unread_q, _ = add_page(journey, page_number=1, status="NEEDS_REVIEW")
    _, in_hand_q, _ = add_page(journey, page_number=2, status="EXTRACTING")
    result = _call(resync_page, journey, queue_id=unread_q)
    assert result["status"] == "SYNCING_TO_AUDIT_CORE"
    assert _page_status(journey, unread_q) == "SYNCING_TO_AUDIT_CORE"
    assert queue_row(journey, "JOURNEY_RECONCILE", str(journey.journey_id))["payload"]["reason"] == "PAGE_RESYNC"
    assert queue_row(journey, "PAGE_REREAD", str(unread_q)) is None
    assert queue_row(journey, "DOCUMENT_INGEST", str(unread_q)) is None
    with pytest.raises(HTTPException) as refused:
        _call(resync_page, journey, queue_id=in_hand_q)
    assert refused.value.status_code == 409


def _evidence(journey, evidence_id):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return dict(connection.execute(
            text("SELECT association_status, void_reason, supersedes_evidence_id FROM auditcore.evidence "
                 "WHERE tenant_id=:t AND evidence_id=:e"),
            {"t": journey.tenant_id, "e": evidence_id},
        ).mappings().one())


def _capture_status(journey, di_document_id):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT capture_status FROM auditcore.document_capture_v2_documents "
                 "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d"),
            {"t": journey.tenant_id, "j": journey.journey_id, "d": di_document_id},
        ).scalar_one()


def test_restore_swaps_the_superseded_copy_back_in(journey):
    _, _, older_doc = add_page(journey, page_number=1, status="READY")
    _, _, newer_doc = add_page(journey, page_number=2, status="READY")
    older = add_evidence(journey, di_document_id=older_doc, document_type_key="pan_card", status="SUPERSEDED")
    newer = add_evidence(journey, di_document_id=newer_doc, document_type_key="pan_card")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        requirement_ref = connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_requirements (
                    tenant_id, journey_id, document_requirement_item_id, requirement_key, document_type_key,
                    process_area, requirement_level, requirement_status, condition_snapshot
                ) VALUES (:t, :j, NULL, 'pan_card', 'pan_card', 'BOOKING', 'REQUIRED', 'PENDING', '{}'::jsonb)
                RETURNING journey_document_requirement_id
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()
        connection.execute(
            text("UPDATE auditcore.evidence SET journey_document_requirement_id=:r, "
                 "supersedes_evidence_id=CASE WHEN evidence_id=:newer THEN :older END "
                 "WHERE tenant_id=:t AND evidence_id IN (:older, :newer)"),
            {"r": requirement_ref, "t": journey.tenant_id, "older": older, "newer": newer},
        )
        connection.execute(
            text("UPDATE auditcore.document_capture_v2_documents SET capture_status='SUPERSEDED' "
                 "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d"),
            {"t": journey.tenant_id, "j": journey.journey_id, "d": older_doc},
        )
    result = _call(restore_document_copy, journey, evidence_id=older)
    assert result == {"evidenceId": str(older), "documentId": str(older_doc), "status": "ACTIVE",
                      "supersededEvidenceId": str(newer)}
    assert _evidence(journey, older) == {"association_status": "ACTIVE", "void_reason": None,
                                         "supersedes_evidence_id": newer}
    assert _evidence(journey, newer)["association_status"] == "SUPERSEDED"
    assert _evidence(journey, newer)["void_reason"] == "EARLIER_COPY_RESTORED"
    assert _capture_status(journey, older_doc) == "CLASSIFIED"
    assert _capture_status(journey, newer_doc) == "SUPERSEDED"
    assert queue_row(journey, "STAGE_RECOMPUTE", str(journey.journey_id))["work_status"] == "PENDING"
    [event] = _activities(journey, "DOCUMENT_COPY_RESTORED")
    assert event["details"]["supersededEvidenceId"] == str(newer)
    # Only a superseded copy can be restored.
    with pytest.raises(HTTPException) as refused:
        _call(restore_document_copy, journey, evidence_id=older)
    assert refused.value.status_code == 409
    with pytest.raises(HTTPException) as missing:
        _call(restore_document_copy, journey, evidence_id=UUID(int=0))
    assert missing.value.status_code == 404


def test_recheck_asks_again_for_the_pages_the_pipeline_owes(journey):
    from audit_core.uc03_p2_deal_actions import recheck_journey

    _, stuck_q, _ = add_page(journey, page_number=1, status="EXTRACTING", submitted_minutes_ago=9 * 60)
    _, waiting_q, _ = add_page(journey, page_number=2, status="EXTRACTING", submitted_minutes_ago=30)
    _set_template(journey, stuck_q, "booking_docket")
    _set_template(journey, waiting_q, "booking_docket")
    result = _call(recheck_journey, journey)
    assert result["pagesReread"] == 1 and result["documentHealth"]["stuck"] == 1
    assert queue_row(journey, "PAGE_REREAD", str(stuck_q))["work_status"] == "PENDING"
    assert queue_row(journey, "PAGE_REREAD", str(waiting_q)) is None
    reconcile = queue_row(journey, "JOURNEY_RECONCILE", str(journey.journey_id))
    assert reconcile["work_status"] == "PENDING" and reconcile["next_attempt_at_utc"] is not None
    assert STUCK_AFTER_SECONDS == 8 * 3600
