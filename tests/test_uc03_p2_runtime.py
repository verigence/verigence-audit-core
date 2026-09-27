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
    ("di_item", "fields", "submitted_ago", "processed_ago", "expected"),
    [
        (None, 3, 1, None, "READY"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSING"}, 0, 1, None, "EXTRACTING"),
        ({"state": "CLASSIFYING"}, 0, 1, None, "CLASSIFYING"),
        ({"state": "UNKNOWN"}, 0, 1, None, "SUPPORTING"),
        ({"state": "FAILED"}, 0, 1, None, "FAILED"),
        ({"state": "CLASSIFIED", "processingStatus": "FAILED"}, 0, 1, None, "FAILED"),
        ({"state": "DELETED"}, 0, 1, None, "CANCELLED"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSED"}, 0, 2, 1, "SYNCING_TO_AUDIT_CORE"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSED"}, 0, 20, 10, "NEEDS_REVIEW"),
        ({"state": "CLASSIFIED", "processingStatus": "PROCESSING"}, 0, 45, None, "FAILED"),
        (None, 0, 1, None, "CLASSIFYING"),
        (None, 0, 10, None, "FAILED"),
    ],
)
def test_page_outcome_never_waits_forever(di_item, fields, submitted_ago, processed_ago, expected):
    outcome = worker.classify_page_outcome(
        di_item=di_item,
        extracted_count=fields,
        submitted_at=NOW - timedelta(minutes=submitted_ago),
        processed_seen_at=NOW - timedelta(minutes=processed_ago) if processed_ago is not None else None,
        now=NOW,
    )
    assert outcome.status == expected
    if expected in {"FAILED", "NEEDS_REVIEW", "CANCELLED", "SUPPORTING"}:
        assert outcome.reason


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
    monkeypatch.setattr(worker, "_MAX_ATTEMPTS", 2)
    _, queue_id, _ = add_page(journey, status="DI_UPLOAD_PREPARING")
    _enqueue(journey, work_type="DOCUMENT_INGEST", key=str(queue_id))

    [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    worker._fail(journey.engine, item, RuntimeError("DI unavailable"))
    row = queue_row(journey, "DOCUMENT_INGEST", str(queue_id))
    assert row["work_status"] == "RETRY_WAIT" and row["attempt_count"] == 1
    assert row["next_attempt_at_utc"] is not None

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_work_queue SET next_attempt_at_utc=now() WHERE work_id=:w"),
            {"w": row["work_id"]},
        )
    [item] = worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
    worker._fail(journey.engine, item, RuntimeError("DI unavailable"))
    assert queue_row(journey, "DOCUMENT_INGEST", str(queue_id))["work_status"] == "DEAD_LETTER"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        page = connection.execute(
            text("SELECT queue_status, status_reason FROM auditcore.p2_document_queue WHERE queue_id=:q"),
            {"q": queue_id},
        ).mappings().one()
    assert page["queue_status"] == "FAILED" and "Retry" in page["status_reason"]


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


def _reconcile(journey, monkeypatch, documents):
    client = FakeCaptureClient(documents)
    monkeypatch.setattr(worker, "_di_context_and_requirements", lambda engine, work: ("ctx", "tok", [], {}))
    monkeypatch.setattr(worker, "get_di_capture_v2_client", lambda: client)
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

    # A second proposal made against 150 goes stale when the value changes first.
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
        competing = correct_p2_document_field(
            tenant_id=journey.tenant_id, journey_id=journey.journey_id, document_id=di_document_id,
            command=P2FieldCorrectionCommand(
                canonicalFieldId=canonical, fieldKey="p2_test_amount",
                sourceFactVersion=1, newValue="160", remarks="other",
            ),
            human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
            connection=connection,
        )
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        submit_action(connection, tenant_id=journey.tenant_id, task_id=competing["taskId"],
                      action="APPROVE_CORRECTION", actor_id="tl-1", actor_role_code="TL", comment=None)
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
