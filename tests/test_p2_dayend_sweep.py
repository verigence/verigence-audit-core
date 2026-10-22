"""The day-end sweep (product owner 2026-10-22).

Every day at 23:30 IST (18:00 UTC) the worker clears from the Journey the
pages that ended FAILED after their retries. The PC's HIGH "Document to
resubmit" task stays open until the page is uploaded again; the stored page
file is never touched (the failure report links to it); the failure report
still lists the page with its reason. Real Postgres."""
from __future__ import annotations

import io
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    MemoryStorage,
    add_batch_pages,
    add_page,
    create_p2_journey,
    database_engine,
)
from pypdf import PdfWriter
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_page_removal import sweep_failed_pages
from audit_core.uc03_p2_task_producer import sync_processing_failure_tasks

DAY = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)  # 23:30 IST
AI_FAILED = "AI reading failed — tried 3 times (at once, then 10 and 20 minutes later). Please upload this page again."


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2sweep")
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, statement, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        result = connection.execute(text(statement), {"t": journey.tenant_id, "j": journey.journey_id, **params})
        return result.mappings().all() if result.returns_rows else None


def _page(journey, queue_id):
    [row] = _sql(journey, "SELECT queue_status, status_reason, last_error, page_object_key, di_document_id "
                          "FROM auditcore.p2_document_queue WHERE queue_id=:q", q=queue_id)
    return row


def _batch_status(journey, batch_id):
    [row] = _sql(journey, "SELECT batch_status FROM auditcore.p2_upload_batches WHERE batch_id=:b", b=batch_id)
    return row["batch_status"]


def _fail(journey, queue_id, code="DOCUMENT_AI_UNAVAILABLE", reason=AI_FAILED):
    _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='FAILED', last_error=:e, status_reason=:r "
                  "WHERE queue_id=:q", q=queue_id, e=code, r=reason)


def _sync(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        sync_processing_failure_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _sweep(journey, now=DAY):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return sweep_failed_pages(connection, tenant_id=journey.tenant_id, now=now)


def _task(journey, batch_id, prefix="resubmit"):
    rows = _sql(journey, "SELECT task_type, task_status, severity, title, description, reference, completion_result "
                         "FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k",
                k=f"{prefix}:{journey.journey_id}:{batch_id}")
    return rows[0] if rows else None


def _gate(journey) -> list[str]:
    from audit_core.uc03_p2_stage import _open_tasks_for

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [t["taskType"] for t in _open_tasks_for(connection, tenant_id=journey.tenant_id,
                                                        journey_id=journey.journey_id, role="PC",
                                                        tabs=("DOCUMENTS", "MANUAL_VERIFICATION"))]


def _hold_documents(journey, rows):
    """Document Intelligence holds a capture row for each page (add_batch_pages
    inserts only the queue rows)."""
    for row in rows:
        _sql(journey, "INSERT INTO auditcore.document_capture_v2_documents (tenant_id, journey_id, stage_code, "
                      "di_document_id, client_upload_id, capture_status, created_by_actor_id) "
                      "VALUES (:t, :j, 'BOOKING', :d, :c, 'CLASSIFYING', :a)",
             d=row["di_document_id"], c=f"cap-{uuid4().hex}", a=journey.actor_id)


def _file(journey, pages):
    """One upload (scan packet) whose pages are in the given states, as they
    stand at 22:00: the file's status is refreshed and its tasks are raised."""
    batch_id, rows = add_batch_pages(journey, pages)
    _hold_documents(journey, rows)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker._refresh_batch_status(connection, journey.tenant_id, batch_id)
    _sync(journey)
    return batch_id, rows


# ------------------------------------------------------------------- what it selects

def test_the_sweep_clears_only_the_failed_pages(journey):
    _, rows = _file(journey, [
        ("booking_form", "READY"), (None, "FAILED"), (None, "QUEUED"), (None, "NEEDS_REVIEW"), (None, "SUPPORTING"),
        (None, "RETRY_WAIT"), (None, "EXTRACTING"), (None, "DEAD_LETTER"), (None, "FAILED"),
    ])
    q = [r["queue_id"] for r in rows]
    _fail(journey, q[1], "NOT_READ_IN_30_MIN", "Not read within 30 minutes. Please upload this page again.")
    _fail(journey, q[7], "DOCUMENT_AI_RESPONSE_INVALID")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='DEAD_LETTER' WHERE queue_id=:q", q=q[7])
    # A page the scan-quality check refused is FAILED too, but is not swept (it has its own task and a Remove card).
    _fail(journey, q[8], "DI_QUALITY_IMAGE_BLUR_SCORE", "Page rejected: blurred.")
    before = {i: _page(journey, qid)["queue_status"] for i, qid in enumerate(q)}

    result = _sweep(journey)

    assert result == {"pages": 2, "journeys": 1, "documentsVoided": 2, "failedJourneys": 0}
    for i, qid in enumerate(q):
        status = _page(journey, qid)["queue_status"]
        assert status == ("CANCELLED" if i in (1, 7) else before[i]), (i, status)
    assert _page(journey, q[8])["queue_status"] == "FAILED"
    # The swept pages keep their code and reason; one PAGE_SWEPT event each.
    swept = _page(journey, q[1])
    assert (swept["last_error"], swept["status_reason"]) == (
        "NOT_READ_IN_30_MIN", "Not read within 30 minutes. Please upload this page again.")
    events = _sql(journey, "SELECT subject_id, details FROM auditcore.p2_activity_events "
                           "WHERE tenant_id=:t AND event_type='PAGE_SWEPT' ORDER BY event_id")
    assert [e["subject_id"] for e in events] == [str(q[1]), str(q[7])]
    assert events[0]["details"]["code"] == "NOT_READ_IN_30_MIN" and events[0]["details"]["pages"] == [2]
    assert events[0]["details"]["by"] == "day-end sweep" and events[0]["details"]["documentVoided"] is True
    # Their DI documents were voided, as deleting a document voids it.
    assert _sql(journey, "SELECT capture_status FROM auditcore.document_capture_v2_documents "
                         "WHERE di_document_id=:d", d=rows[1]["di_document_id"])[0]["capture_status"] == "SUPERSEDED"
    assert _sql(journey, "SELECT capture_status FROM auditcore.document_capture_v2_documents "
                         "WHERE di_document_id=:d", d=rows[0]["di_document_id"])[0]["capture_status"] == "CLASSIFYING"


def test_a_journey_that_is_completed_or_cancelled_is_not_swept(journey):
    _, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"])
    _sql(journey, "UPDATE auditcore.journeys SET review_completed_at_utc=now() WHERE tenant_id=:t AND journey_id=:j")
    assert _sweep(journey)["pages"] == 0
    assert _page(journey, rows[1]["queue_id"])["queue_status"] == "FAILED"
    _sql(journey, "UPDATE auditcore.journeys SET review_completed_at_utc=NULL WHERE tenant_id=:t AND journey_id=:j")
    for status, disposition in (("BOOKING_CANCELLED", None), ("DUPLICATE_BOOKING", None), (None, "NO_DELIVERY")):
        _sql(journey, "DELETE FROM auditcore.journey_stage_states WHERE tenant_id=:t AND journey_id=:j")
        _sql(journey, "INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, "
                      "business_status, closure_disposition) VALUES (:t, :j, 'BOOKING', :s, :d)",
             s=status, d=disposition)
        assert _sweep(journey)["pages"] == 0, (status, disposition)
        assert _page(journey, rows[1]["queue_id"])["queue_status"] == "FAILED"
    _sql(journey, "DELETE FROM auditcore.journey_stage_states WHERE tenant_id=:t AND journey_id=:j")
    assert _sweep(journey)["pages"] == 1  # an open Journey is swept


def test_pages_failing_in_two_journeys_are_swept_each_in_its_own_transaction(journey, monkeypatch):
    with journey.engine.begin() as connection:
        second = UUID(str(connection.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference) "
                 "VALUES (:t, :d, :o, :c, :r) RETURNING journey_id"),
            {"t": journey.tenant_id, "d": journey.dealer_id, "o": journey.outlet_id, "c": journey.customer_id,
             "r": f"P2-J-second-{uuid4().hex[:6]}"},
        ).scalar_one()))
    _, rows_a = _file(journey, [("booking_form", "FAILED")])
    _fail(journey, rows_a[0]["queue_id"])
    other = SimpleNamespace(engine=journey.engine, tenant_id=journey.tenant_id, journey_id=second,
                            actor_id=journey.actor_id)
    _, rows_b = add_batch_pages(other, [("booking_form", "FAILED")])
    _fail(journey, rows_b[0]["queue_id"])
    from audit_core import uc03_p2_page_removal as removal

    real = removal.take_page_off_journey

    def flaky(connection, **kwargs):
        if kwargs["journey_id"] == second:
            raise RuntimeError("boom")
        return real(connection, **kwargs)

    monkeypatch.setattr(removal, "take_page_off_journey", flaky)
    result = _sweep(journey)
    assert result["pages"] == 1 and result["failedJourneys"] == 1
    assert _page(journey, rows_a[0]["queue_id"])["queue_status"] == "CANCELLED"
    assert _page(journey, rows_b[0]["queue_id"])["queue_status"] == "FAILED"  # rolled back, swept by the next run
    monkeypatch.setattr(removal, "take_page_off_journey", real)
    assert _sweep(journey)["pages"] == 1
    assert _page(journey, rows_b[0]["queue_id"])["queue_status"] == "CANCELLED"


# ------------------------------------------------------------------- the resubmit task

def test_the_high_resubmit_task_stays_open_after_the_sweep_unlike_a_persons_removal(journey):
    batch_id, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"], "NOT_READ_IN_30_MIN", "Not read within 30 minutes. Please upload this page again.")
    _fail(journey, rows[2]["queue_id"])
    _sync(journey)
    before = _task(journey, batch_id)
    assert (before["task_type"], before["task_status"], before["severity"]) == ("DOCUMENT_RESUBMIT", "READY", "HIGH")
    assert _gate(journey) == ["DOCUMENT_RESUBMIT"]

    _sweep(journey)

    after = _task(journey, batch_id)
    assert (after["task_type"], after["task_status"], after["severity"]) == ("DOCUMENT_RESUBMIT", "READY", "HIGH")
    assert after["completion_result"] in (None, {}) or "reason" not in after["completion_result"]
    # It still names every swept page, its reason and the link that opens it.
    assert after["title"] == before["title"] == "packet.pdf: 2 pages to resubmit"
    assert after["reference"]["pageNumbers"] == [2, 3] and after["reference"]["category"] == "DOCUMENT_RESUBMIT"
    assert [p["code"] for p in after["reference"]["pages"]] == ["NOT_READ_IN_30_MIN", "DOCUMENT_AI_UNAVAILABLE"]
    assert after["reference"]["pages"][0]["filePath"].endswith(f"/pages/{rows[1]['queue_id']}/file")
    assert after["reference"]["pages"][0]["reason"] == "Not read within 30 minutes. Please upload this page again."
    assert _gate(journey) == ["DOCUMENT_RESUBMIT"]  # still blocks the gate until the pages are uploaded again
    # A later sync (any worker pass, a new upload) does not close it either.
    _sync(journey)
    assert _task(journey, batch_id)["task_status"] == "READY"
    # The file is not "completed": two of its three pages are waiting to be resubmitted.
    assert _batch_status(journey, batch_id) == "PARTIAL_FAILURE"

    # Contrast: a person's removal (#480) of a failed page closes its task.
    other_batch, other = _file(journey, [("booking_form", "READY"), (None, "FAILED")])
    _fail(journey, other[1]["queue_id"])
    _sync(journey)
    assert _task(journey, other_batch)["task_status"] == "READY"
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    removed = TestClient(app, raise_server_exceptions=False).post(
        f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/pages/{other[1]['queue_id']}:remove",
        json={"reason": "Not needed"})
    assert removed.status_code == 200, removed.text
    assert _task(journey, other_batch)["task_status"] == "VERIFIED_COMPLETE"


def test_a_setup_problem_page_is_swept_like_any_other_and_its_task_stays_high(journey):
    batch_id, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"], "DOCUMENT_AI_NOT_CONFIGURED", worker._SETUP_REASON)
    _sync(journey)
    _sweep(journey)
    task = _task(journey, batch_id)
    assert (task["task_status"], task["severity"]) == ("READY", "HIGH")
    assert "Our setup problem" in task["description"] and "overnight" not in task["description"]


def test_a_reupload_of_the_swept_file_closes_the_task(journey, monkeypatch):
    """The whole flow with the real split: the page fails, the sweep clears it,
    the task stays open, the PC uploads the file again (a file with failed
    pages may be uploaded again whole), the failed page's replacement arrives
    and the task closes."""
    storage = MemoryStorage()
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(worker, "_owned", lambda *args, **kwargs: None)
    payload = _pdf(200, 300)
    first = _upload(journey, storage, "scan.pdf", payload)
    _split(journey, first)
    ids = _sql(journey, "SELECT queue_id, page_number, page_sha256 FROM auditcore.p2_document_queue "
                        "WHERE batch_id=:b ORDER BY page_number", b=first)
    _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='READY' WHERE queue_id=:q", q=ids[0]["queue_id"])
    _fail(journey, ids[1]["queue_id"])
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker._refresh_batch_status(connection, journey.tenant_id, first)
    _sync(journey)
    assert _task(journey, first)["task_status"] == "READY"

    assert _sweep(journey)["pages"] == 1
    assert _page(journey, ids[1]["queue_id"])["queue_status"] == "CANCELLED"
    assert _task(journey, first)["task_status"] == "READY"
    assert _batch_status(journey, first) == "PARTIAL_FAILURE"  # not COMPLETED: a re-upload is not "a duplicate"

    again = _upload(journey, storage, "scan (resubmitted).pdf", payload)
    _split(journey, again)
    assert _batch_status(journey, again) != "CANCELLED", "a file with swept pages is not refused as a duplicate"
    pages = _sql(journey, "SELECT page_number, queue_status, status_reason FROM auditcore.p2_document_queue "
                          "WHERE batch_id=:b ORDER BY page_number", b=again)
    assert pages[0]["queue_status"] == "CANCELLED" and pages[0]["status_reason"].startswith("Same as page 1")
    assert pages[1]["queue_status"] == "QUEUED", "the replacement of the failed page is read"
    old = _page(journey, ids[1]["queue_id"])
    assert (old["queue_status"], old["last_error"]) == ("CANCELLED", "SUPERSEDED_PAGE")
    task = _task(journey, first)
    assert task["task_status"] == "VERIFIED_COMPLETE"
    assert task["completion_result"]["reason"] == "The pages were read again, uploaded again or removed."
    assert _gate(journey) == []
    assert _batch_status(journey, first) == "COMPLETED"
    # A second sweep finds nothing left to do.
    assert _sweep(journey)["pages"] == 0


def test_a_person_can_remove_a_swept_page_when_a_different_scan_was_uploaded(journey):
    """A re-scan is different bytes: nothing recognises it as the replacement. The person
    removes the swept page, as they could remove the failed page before the sweep."""
    batch_id, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"])
    _sync(journey)
    _sweep(journey)
    assert _task(journey, batch_id)["task_status"] == "READY"
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/pages/{rows[1]['queue_id']}:remove"
    assert client.post(url, json={"reason": "x"}).status_code == 400
    response = client.post(url, json={"reason": "Re-scan uploaded as scan2.pdf"})
    assert response.status_code == 200, response.text
    assert response.json() == {"queueId": str(rows[1]["queue_id"]), "status": "CANCELLED", "documentVoided": False}
    task = _task(journey, batch_id)
    assert task["task_status"] == "VERIFIED_COMPLETE"
    assert "removed from the journey by" in task["completion_result"]["reason"]
    assert _page(journey, rows[1]["queue_id"])["last_error"] == "SWEPT_PAGE_REMOVED"
    assert _batch_status(journey, batch_id) == "COMPLETED"
    # Once resolved it can neither be removed again nor swept again.
    assert client.post(url, json={"reason": "again"}).status_code == 409
    assert _sweep(journey)["pages"] == 0


# ------------------------------------------------------------------- the failure report and the stored file

class SpyStorage:
    """Storage that signs links and refuses anything else: a delete (or any
    method the P2 code has no business calling) fails the test."""

    def __init__(self):
        self.signed: list[str] = []

    def presign_get(self, object_key, *, expires_seconds=1800):
        self.signed.append(object_key)
        return f"https://signed.example/{object_key}?expires={expires_seconds}"

    def __getattr__(self, name):
        raise AssertionError(f"storage.{name} must not be called by the sweep")


def _tl(journey) -> str:
    tl = f"tl-{uuid4().hex[:8]}"
    with journey.engine.begin() as connection:
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                 "VALUES (:t, :a, 'TL')"),
            {"t": journey.tenant_id, "a": tl},
        )
    return tl


def _report(journey, tl, path="/reports/failed-pages"):
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=tl)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    return TestClient(app, raise_server_exceptions=False).get(f"/p2/v1/tenants/{journey.tenant_id}{path}")


def test_the_stored_file_is_kept_and_the_failure_report_still_lists_the_swept_page(journey, monkeypatch):
    import audit_core.uc03_p2_api as api
    import audit_core.uc03_p2_storage as storage_module

    spy = SpyStorage()
    monkeypatch.setattr(storage_module, "get_p2_document_storage", lambda: spy)
    monkeypatch.setattr(api, "get_p2_document_storage", lambda: spy)
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: spy)
    tl = _tl(journey)
    _, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"], "DOCUMENT_AI_RESPONSE_INVALID", AI_FAILED)
    key = rows[1]["object_key"]

    [listed_before] = _report(journey, tl).json()["rows"]
    _sweep(journey)

    assert _page(journey, rows[1]["queue_id"])["page_object_key"] == key  # the stored file's key is kept
    response = _report(journey, tl)
    assert response.status_code == 200, response.text
    [row] = response.json()["rows"]
    assert row["queueId"] == str(rows[1]["queue_id"]) and row["pageNumbers"] == [2] and row["kind"] == "PAGE"
    assert row["reason"] == AI_FAILED and row["code"] == "DOCUMENT_AI_RESPONSE_INVALID"
    assert row["openUrl"] == f"https://signed.example/{key}?expires=1800"  # a presigned link, not the bucket
    assert row["clearedFromJourney"] is True and listed_before["clearedFromJourney"] is False
    assert row["failedAtUtc"] == listed_before["failedAtUtc"], "the day it failed, not the sweep time"
    assert row["filePath"] == listed_before["filePath"]
    # The page-file endpoint still issues a link for the swept page.
    link = TestClient(app, raise_server_exceptions=False).get(
        f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/pages/{rows[1]['queue_id']}/file")
    assert link.status_code == 200 and link.json()["url"] == f"https://signed.example/{key}?expires=1800"
    # The daily report counts it as a failed document, the same before and after.
    from audit_core.uc03_p2_daily_report import build_daily_processing_report

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        day = connection.execute(text("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date")).scalar_one()
        report = build_daily_processing_report(connection, tenant_id=journey.tenant_id, actor_id=tl, day=day)
    assert [p["queueId"] for p in report["failedPages"]] == [str(rows[1]["queue_id"])]
    assert report["failedPages"][0]["clearedFromJourney"] is True
    [daily] = [j for j in report["journeys"] if j["files"] == 1]
    assert daily["documents"]["failed"] == 1, daily  # still one failed document after the sweep

def test_a_resolved_swept_page_leaves_the_failure_report(journey, monkeypatch):
    import audit_core.uc03_p2_storage as storage_module

    monkeypatch.setattr(storage_module, "get_p2_document_storage", lambda: SpyStorage())
    tl = _tl(journey)
    _, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"])
    _sweep(journey)
    assert len(_report(journey, tl).json()["rows"]) == 1
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='SUPERSEDED_PAGE' WHERE queue_id=:q",
         q=rows[1]["queue_id"])
    assert _report(journey, tl).json()["rows"] == []


# ------------------------------------------------------------------- idempotent, once a day

def test_the_sweep_is_idempotent(journey):
    _, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"])
    _fail(journey, rows[2]["queue_id"])
    assert _sweep(journey)["pages"] == 2
    snapshot = _sql(journey, "SELECT queue_id, queue_status, status_reason, last_error, updated_at_utc "
                             "FROM auditcore.p2_document_queue ORDER BY page_number")
    events = _sql(journey, "SELECT count(*) AS n FROM auditcore.p2_activity_events WHERE event_type='PAGE_SWEPT'")
    assert _sweep(journey) == {"pages": 0, "journeys": 0, "documentsVoided": 0, "failedJourneys": 0}
    assert _sql(journey, "SELECT queue_id, queue_status, status_reason, last_error, updated_at_utc "
                         "FROM auditcore.p2_document_queue ORDER BY page_number") == snapshot
    assert _sql(journey, "SELECT count(*) AS n FROM auditcore.p2_activity_events "
                         "WHERE event_type='PAGE_SWEPT'") == events


def _claims(journey):
    return [r["run_key"] for r in _sql(journey, "SELECT run_key FROM auditcore.p2_scheduled_runs "
                                                "WHERE tenant_id=:t AND run_name='P2_DAYEND_SWEEP' ORDER BY run_key")]


def test_it_runs_once_a_day_from_the_configured_time(journey, monkeypatch):
    assert worker._DAYEND_SWEEP_DEFAULT_UTC == "18:00" and worker._DAYEND_SWEEP_UTC == "18:00"
    # 18:00 UTC is 23:30 IST.
    assert DAY.astimezone(timezone_ist()).strftime("%H:%M") == "23:30"
    _, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"])
    worker._last_dayend_sweep.pop(journey.tenant_id, None)

    assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY - timedelta(minutes=1)) is None
    assert _page(journey, rows[1]["queue_id"])["queue_status"] == "FAILED" and _claims(journey) == []
    result = worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY)
    assert result["pages"] == 1 and _claims(journey) == ["2026-10-22"]
    # Later the same day -- this worker, a restarted worker, another instance -- nothing runs again.
    _fail_again = add_page(journey, page_number=9, status="FAILED")[1]
    assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY + timedelta(hours=3)) is None
    worker._last_dayend_sweep.pop(journey.tenant_id, None)  # as after a restart
    assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY + timedelta(hours=3)) is None
    assert _page(journey, _fail_again)["queue_status"] == "FAILED"
    assert _claims(journey) == ["2026-10-22"]
    # The next day it runs again and takes what failed since.
    tomorrow = DAY + timedelta(days=1)
    result = worker.run_dayend_sweep(journey.engine, journey.tenant_id, tomorrow)
    assert result["pages"] == 1 and _page(journey, _fail_again)["queue_status"] == "CANCELLED"
    assert _claims(journey) == ["2026-10-22", "2026-10-23"]


def timezone_ist():
    from datetime import timezone

    return timezone(timedelta(hours=5, minutes=30))


def test_a_worker_that_was_down_at_the_time_still_runs_the_days_sweep_when_back(journey):
    _, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"])
    worker._last_dayend_sweep.pop(journey.tenant_id, None)
    assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY + timedelta(hours=5))["pages"] == 1


def test_two_worker_instances_never_run_the_day_twice(journey, monkeypatch):
    _, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"])
    _fail(journey, rows[2]["queue_id"])
    from audit_core import uc03_p2_page_removal as removal

    real = removal.sweep_failed_pages
    calls: list[str] = []

    def slow(connection, **kwargs):
        calls.append(threading.current_thread().name)
        time.sleep(0.5)  # the other instance arrives while this one is still sweeping
        return real(connection, **kwargs)

    monkeypatch.setattr(removal, "sweep_failed_pages", slow)
    results: dict[str, object] = {}
    worker._last_dayend_sweep.pop(journey.tenant_id, None)

    def instance(name):
        results[name] = worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY)

    threads = [threading.Thread(target=instance, args=(n,), name=n) for n in ("worker-1", "worker-2")]
    for t in threads:
        t.start()
        time.sleep(0.1)  # the second instance arrives while the first is still sweeping
    for t in threads:
        t.join()
    finished = [r for r in results.values() if r is not None]
    assert len(finished) == 1 and finished[0]["pages"] == 2, results
    assert len(calls) == 1, "the second instance lost the claim and never swept"
    assert _claims(journey) == ["2026-10-22"]
    assert len(_sql(journey, "SELECT 1 FROM auditcore.p2_activity_events WHERE event_type='PAGE_SWEPT'")) == 2


def test_the_time_is_configurable_and_a_typo_falls_back_to_the_default(monkeypatch):
    assert worker._hhmm("19:15", "18:00") == "19:15" and worker._hhmm(" 7:05 ", "18:00") == "07:05"
    for bad in ("", "noon", "25:00", "18:60", "18", "18:xx", "-1:00"):
        assert worker._hhmm(bad, "18:00") == "18:00", bad
    monkeypatch.setattr(worker, "_DAYEND_SWEEP_UTC", "19:15")
    worker._last_dayend_sweep.pop("tenant-x", None)
    assert worker._dayend_sweep_due("tenant-x", datetime(2026, 10, 22, 19, 14, tzinfo=UTC)) is None
    assert worker._dayend_sweep_due("tenant-x", datetime(2026, 10, 22, 19, 15, tzinfo=UTC)) == "2026-10-22"


def test_the_worker_loop_runs_the_sweep_and_the_delivery_review_is_a_separate_job(journey, monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(worker, "run_dayend_sweep", lambda engine, tenant_id: ran.append(tenant_id))
    monkeypatch.setattr(worker, "_fact_sweep", lambda engine, tenant_id: 0)
    monkeypatch.setattr(worker, "backfill_price_variance", lambda engine, tenant_id: 0)
    monkeypatch.setattr(worker, "fail_unsettled_pages", lambda connection, **kw: 0)
    monkeypatch.setattr(worker, "_NIGHTLY_REVIEW_UTC", "23:59")  # not due: the sweep must run before that early return
    worker._last_sweep_at.pop(journey.tenant_id, None)
    worker._last_watchdog_at.pop(journey.tenant_id, None)
    worker._maybe_sweep(journey.engine, journey.tenant_id)
    assert ran == [journey.tenant_id]


# ------------------------------------------------------------------- helpers for the real split

def _pdf(*widths: int) -> bytes:
    writer = PdfWriter()
    for width in widths:
        writer.add_blank_page(width=width, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _upload(journey, storage, filename, payload):
    batch_id = uuid4()
    key = f"p2/{batch_id}/original"
    storage.objects[key] = payload
    _sql(journey, "INSERT INTO auditcore.p2_upload_batches (tenant_id, batch_id, journey_id, original_filename, "
                  "content_type, size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id) "
                  "VALUES (:t, :b, :j, :f, 'application/pdf', :n, 0, :k, 'UPLOADED', :a)",
         b=batch_id, f=filename, n=len(payload), k=key, a=journey.actor_id)
    return batch_id


def _split(journey, batch_id):
    work = worker.WorkItem(journey.tenant_id, uuid4(), journey.journey_id, "SPLIT_BATCH", str(batch_id), {}, 0, None,
                           None, uuid4())
    worker._split_batch(journey.engine, work)


