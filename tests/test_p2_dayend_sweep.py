"""The day-end sweep (product owner 2026-10-22).

Every night at 02:00 IST (20:30 UTC) the worker clears from the Journey the
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

from audit_core import uc03_p2_runtime as runtime
from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_page_removal import sweep_failed_pages
from audit_core.uc03_p2_task_producer import sync_processing_failure_tasks

DAY = datetime(2026, 10, 22, 20, 30, tzinfo=UTC)  # 02:00 IST on the 23rd; claimed as "2026-10-22"
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
    # A page the scan-quality check refused is FAILED too and is swept like the others (2026-10-22).
    _fail(journey, q[8], "DI_QUALITY_IMAGE_BLUR_SCORE", "Page rejected: blurred.")
    before = {i: _page(journey, qid)["queue_status"] for i, qid in enumerate(q)}

    result = _sweep(journey)

    assert result == {"pages": 3, "journeys": 1, "documentsVoided": 3, "failedJourneys": 0}
    for i, qid in enumerate(q):
        status = _page(journey, qid)["queue_status"]
        assert status == ("CANCELLED" if i in (1, 7, 8) else before[i]), (i, status)
    blurred = _page(journey, q[8])
    assert (blurred["last_error"], blurred["status_reason"]) == ("DI_QUALITY_IMAGE_BLUR_SCORE", "Page rejected: blurred.")
    # The swept pages keep their code and reason; one PAGE_SWEPT event each.
    swept = _page(journey, q[1])
    assert (swept["last_error"], swept["status_reason"]) == (
        "NOT_READ_IN_30_MIN", "Not read within 30 minutes. Please upload this page again.")
    events = _sql(journey, "SELECT subject_id, details FROM auditcore.p2_activity_events "
                           "WHERE tenant_id=:t AND event_type='PAGE_SWEPT' ORDER BY event_id")
    assert [e["subject_id"] for e in events] == [str(q[1]), str(q[7]), str(q[8])]
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


def test_it_runs_once_a_night_from_the_configured_time(journey, monkeypatch):
    assert worker._DAYEND_SWEEP_DEFAULT_UTC == runtime.DAYEND_SWEEP_DEFAULT_UTC == "20:30"
    assert runtime.DAYEND_SWEEP_UTC == "20:30" and runtime.dayend_sweep_ist() == "02:00 IST"
    # 20:30 UTC on the 22nd is 02:00 IST on the 23rd: the night is named by the UTC date it starts on.
    assert DAY.astimezone(timezone_ist()).strftime("%Y-%m-%d %H:%M") == "2026-10-23 02:00"
    _, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"])
    worker._last_dayend_sweep.pop(journey.tenant_id, None)

    assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY - timedelta(minutes=1)) is None
    assert _page(journey, rows[1]["queue_id"])["queue_status"] == "FAILED" and _claims(journey) == []
    result = worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY)
    assert result["pages"] == 1 and _claims(journey) == ["2026-10-22"]
    # Later the same night -- this worker, a restarted worker, another instance -- nothing runs again,
    # even across midnight UTC (00:30 UTC on the 23rd is still the night of the 22nd's slot).
    late = add_page(journey, page_number=9, status="FAILED")[1]
    for later in (DAY + timedelta(hours=1), DAY + timedelta(hours=4), DAY + timedelta(hours=5, minutes=59)):
        assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, later) is None
        worker._last_dayend_sweep.pop(journey.tenant_id, None)  # as after a restart
        assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, later) is None
    # A page that failed after the sweep waits for the next night's.
    assert _page(journey, late)["queue_status"] == "FAILED"
    assert _claims(journey) == ["2026-10-22"]
    assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY + timedelta(hours=12)) is None
    # The next night it runs again and takes what failed since.
    result = worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY + timedelta(days=1))
    assert result["pages"] == 1 and _page(journey, late)["queue_status"] == "CANCELLED"
    assert _claims(journey) == ["2026-10-22", "2026-10-23"]


def timezone_ist():
    from datetime import timezone

    return timezone(timedelta(hours=5, minutes=30))


def test_a_page_failing_between_the_sweep_and_midnight_utc_waits_for_the_next_night(journey):
    _, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"])
    worker._last_dayend_sweep.pop(journey.tenant_id, None)
    assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY)["pages"] == 1
    after = add_page(journey, page_number=9, status="FAILED")[1]  # fails at, say, 22:00 UTC / 03:30 IST
    for now in (DAY + timedelta(hours=1, minutes=30), DAY + timedelta(hours=3, minutes=29)):
        assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, now) is None
    assert _page(journey, after)["queue_status"] == "FAILED"
    assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY + timedelta(days=1))["pages"] == 1
    assert _page(journey, after)["queue_status"] == "CANCELLED"


def test_a_worker_that_was_down_at_the_time_runs_the_missed_sweep_when_back_but_not_much_later(journey):
    _, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"])
    worker._last_dayend_sweep.pop(journey.tenant_id, None)
    # Back after midnight UTC, still inside the 6 hours: it runs the 22nd's slot.
    assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY + timedelta(hours=5))["pages"] == 1
    assert _claims(journey) == ["2026-10-22"]
    # Down for longer than the catch-up: that night is skipped, the next slot takes the page.
    late = add_page(journey, page_number=9, status="FAILED")[1]
    worker._last_dayend_sweep.pop(journey.tenant_id, None)
    assert worker.run_dayend_sweep(journey.engine, journey.tenant_id,
                                   DAY + timedelta(days=1, hours=6)) is None
    assert _page(journey, late)["queue_status"] == "FAILED" and _claims(journey) == ["2026-10-22"]
    assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY + timedelta(days=2))["pages"] == 1
    assert _claims(journey) == ["2026-10-22", "2026-10-24"]


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
    assert worker._hhmm("19:15", "20:30") == "19:15" and worker._hhmm(" 7:05 ", "20:30") == "07:05"
    for bad in ("", "noon", "25:00", "18:60", "18", "18:xx", "-1:00"):
        assert worker._hhmm(bad, "20:30") == "20:30", bad
    monkeypatch.setattr(runtime, "DAYEND_SWEEP_UTC", "19:15")
    assert runtime.dayend_sweep_ist() == "00:45 IST"  # the PC's message follows the configured time
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




# ------------------------------------------------------------------- pages refused for image quality

BLUR = "DI_QUALITY_IMAGE_BLUR_SCORE"
NOTICE = ("This page will be removed by the system at the day-end clean-up (02:00 IST). "
          "Please upload a clearer copy.")
INSTRUCTION = ('Once uploaded, come back and close the task with the comment "Document Uploaded". '
               'If the document does not need to be uploaded again, close the task with the comment '
               '"Document Not required to be uploaded for Extraction".')


def _tasks(journey, prefix):
    return _sql(journey, "SELECT dedupe_key, task_status FROM auditcore.p2_tasks "
                         "WHERE tenant_id=:t AND dedupe_key LIKE :k", k=f"{prefix}:{journey.journey_id}:%")


def test_a_swept_image_quality_page_keeps_its_task_open_and_says_the_system_removes_it(journey, monkeypatch):
    batch_id, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"], BLUR, "Page rejected: blurred. Re-scan this page and upload it again.")
    _fail(journey, rows[2]["queue_id"])  # an AI failure on the same file: the resubmit task, not the quality one
    _sync(journey)
    before = _task(journey, batch_id, "upload-quality")
    assert before["task_status"] == "READY" and NOTICE in before["description"]
    # What the PC does to close it: both phrases exactly, as guidance (not a fixed list).
    assert INSTRUCTION in before["description"]
    assert before["description"].count('"') == 4
    assert before["description"].startswith("Refused by the scan-quality check and not read: Page 2 is blurred. "
                                            "A retry will not help. " + NOTICE)

    result = _sweep(journey)

    assert result["pages"] == 2
    page = _page(journey, rows[1]["queue_id"])
    assert (page["queue_status"], page["last_error"]) == ("CANCELLED", BLUR)  # off the Journey, code kept
    after = _task(journey, batch_id, "upload-quality")
    assert (after["task_type"], after["task_status"], after["severity"]) == ("PC_PAGE_QUALITY_REJECTED", "READY", "HIGH")
    assert after["completion_result"] in (None, {})
    assert NOTICE in after["description"]
    assert after["reference"]["pageNumbers"] == [2]
    assert after["reference"]["pageQueues"] == [{"page": 2, "queueId": str(rows[1]["queue_id"])}]
    # No double task: one quality task for the refused page and one resubmit task for the other, nothing more.
    assert len(_tasks(journey, "upload-quality")) == 1 and len(_tasks(journey, "resubmit")) == 1
    assert _task(journey, batch_id)["reference"]["pageNumbers"] == [3]
    assert sorted(_gate(journey)) == ["DOCUMENT_RESUBMIT", "PC_PAGE_QUALITY_REJECTED"]
    assert _batch_status(journey, batch_id) == "PARTIAL_FAILURE"
    # Idempotent: a second sweep and a further sync change nothing and raise nothing.
    assert _sweep(journey) == {"pages": 0, "journeys": 0, "documentsVoided": 0, "failedJourneys": 0}
    _sync(journey)
    assert len(_tasks(journey, "upload-quality")) == 1 and len(_tasks(journey, "resubmit")) == 1
    assert _task(journey, batch_id, "upload-quality")["task_status"] == "READY"
    assert len(_sql(journey, "SELECT 1 FROM auditcore.p2_activity_events WHERE event_type='PAGE_SWEPT'")) == 2

    # The card the PC reads says the same, and the time follows the configured sweep time.
    from audit_core.uc03_p2_document_task_groups import document_detail

    card = document_detail({"task_type": "PC_PAGE_QUALITY_REJECTED", "source_code": "DOCUMENT_QUALITY_REJECTED",
                            "reference": after["reference"]}, "IMAGE_QUALITY")
    assert card["reason"] == f"Page 2 is blurred. Re-scan and upload again. {NOTICE} {INSTRUCTION}"
    monkeypatch.setattr(runtime, "DAYEND_SWEEP_UTC", "19:15")
    _sync(journey)
    assert "(00:45 IST)" in _task(journey, batch_id, "upload-quality")["description"]


def test_several_swept_quality_pages_get_the_plural_message(journey):
    batch_id, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"], BLUR)
    _fail(journey, rows[2]["queue_id"], "DI_QUALITY_IMAGE_CONTRAST")
    _sync(journey)
    _sweep(journey)
    task = _task(journey, batch_id, "upload-quality")
    assert task["task_status"] == "READY" and task["reference"]["pageNumbers"] == [2, 3]
    assert ("These pages will be removed by the system at the day-end clean-up (02:00 IST). "
            "Please upload clearer copies.") in task["description"]
    assert INSTRUCTION in task["description"]
    # (_file raised a resubmit task while the pages carried no code; it closed once they were refused for quality.)
    assert [t for t in _tasks(journey, "resubmit") if t["task_status"] != "VERIFIED_COMPLETE"] == []


def test_the_quality_task_closes_when_the_same_scan_is_uploaded_again(journey, monkeypatch):
    """The one replacement path that exists: the page-hash supersede (the same
    bytes). It sees the swept page as it saw the failed one."""
    storage = MemoryStorage()
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(worker, "_owned", lambda *args, **kwargs: None)
    payload = _pdf(210, 310)
    first = _upload(journey, storage, "scan.pdf", payload)
    _split(journey, first)
    ids = _sql(journey, "SELECT queue_id FROM auditcore.p2_document_queue WHERE batch_id=:b ORDER BY page_number",
               b=first)
    _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='READY' WHERE queue_id=:q", q=ids[0]["queue_id"])
    _fail(journey, ids[1]["queue_id"], BLUR)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker._refresh_batch_status(connection, journey.tenant_id, first)
    _sync(journey)
    assert _sweep(journey)["pages"] == 1
    assert _task(journey, first, "upload-quality")["task_status"] == "READY"

    again = _upload(journey, storage, "scan again.pdf", payload)
    _split(journey, again)

    old = _page(journey, ids[1]["queue_id"])
    assert (old["queue_status"], old["last_error"]) == ("CANCELLED", "SUPERSEDED_PAGE")
    assert _task(journey, first, "upload-quality")["task_status"] == "VERIFIED_COMPLETE"
    assert _gate(journey) == []
    assert _sweep(journey)["pages"] == 0


def test_a_different_scan_does_not_close_the_quality_task_the_pc_or_the_remove_card_does(journey, monkeypatch):
    """A re-scan is different bytes and was refused before it was classified, so
    nothing says which page it replaces: another upload never closes the task.
    The system cannot know, so the PC closes it by hand with a comment (tests
    below); the Remove card closes it too."""
    storage = MemoryStorage()
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(worker, "_owned", lambda *args, **kwargs: None)
    first = _upload(journey, storage, "scan.pdf", _pdf(210, 310))
    _split(journey, first)
    ids = _sql(journey, "SELECT queue_id FROM auditcore.p2_document_queue WHERE batch_id=:b ORDER BY page_number",
               b=first)
    _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='READY' WHERE queue_id=:q", q=ids[0]["queue_id"])
    _fail(journey, ids[1]["queue_id"], BLUR)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker._refresh_batch_status(connection, journey.tenant_id, first)
    _sync(journey)
    _sweep(journey)

    rescan = _upload(journey, storage, "rescan.pdf", _pdf(250, 350))  # different bytes
    _split(journey, rescan)
    _sync(journey)
    assert _task(journey, first, "upload-quality")["task_status"] == "READY"
    assert _page(journey, ids[1]["queue_id"])["last_error"] == BLUR

    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    url = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/pages/{ids[1]['queue_id']}:remove"
    response = TestClient(app, raise_server_exceptions=False).post(url, json={"reason": "Re-scan uploaded as rescan.pdf"})
    assert response.status_code == 200, response.text
    task = _task(journey, first, "upload-quality")
    assert task["task_status"] == "VERIFIED_COMPLETE"
    assert "was removed from the journey by" in task["completion_result"]["reason"]
    assert _page(journey, ids[1]["queue_id"])["last_error"] == "SWEPT_PAGE_REMOVED"
    assert _gate(journey) == []


def test_the_failure_report_lists_a_swept_quality_page_with_its_reason_and_link(journey, monkeypatch):
    import audit_core.uc03_p2_api as api
    import audit_core.uc03_p2_storage as storage_module

    spy = SpyStorage()
    monkeypatch.setattr(storage_module, "get_p2_document_storage", lambda: spy)
    monkeypatch.setattr(api, "get_p2_document_storage", lambda: spy)
    tl = _tl(journey)
    _, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"], BLUR, "Page rejected: blurred. Re-scan this page and upload it again.")
    _sweep(journey)
    [row] = _report(journey, tl).json()["rows"]
    assert row["queueId"] == str(rows[1]["queue_id"]) and row["code"] == BLUR
    assert row["reason"] == "Page rejected: blurred. Re-scan this page and upload it again."
    assert row["openUrl"] == f"https://signed.example/{rows[1]['object_key']}?expires=1800"
    assert row["clearedFromJourney"] is True


def test_the_nightly_review_at_the_same_time_leaves_swept_pages_and_their_tasks_alone(journey):
    """Both jobs default to 20:30 UTC. They have their own claims
    (P2_DAYEND_SWEEP, P2_NIGHTLY_REVIEW); the sweep runs first in the worker pass,
    and the review's upload sweep only looks at pages still in flight or read."""
    batch_id, rows = _file(journey, [("booking_form", "SUPPORTING"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"])
    _sync(journey)
    worker._last_dayend_sweep.pop(journey.tenant_id, None)
    assert worker.run_dayend_sweep(journey.engine, journey.tenant_id, DAY)["pages"] == 1
    before = _page(journey, rows[1]["queue_id"])
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert worker.queue_nightly_upload_sweep(connection, tenant_id=journey.tenant_id,
                                                 now=DAY + timedelta(days=2)) == {"reconciled": 0, "journeys": 0}
    assert _page(journey, rows[1]["queue_id"]) == before
    assert _task(journey, batch_id)["task_status"] == "READY"
    assert worker._NIGHTLY_REVIEW_UTC == "20:30" and runtime.DAYEND_SWEEP_UTC == "20:30"
    assert _claims(journey) == ["2026-10-22"]


# ------------------------------------------- the PC closes the image-quality task by hand (2026-10-22)

CLOSE = "CLOSE_TASK"
REASON = "Document Uploaded"


def _quality_task(journey, *, sweep=False):
    batch_id, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"], BLUR, "Page rejected: blurred. Re-scan this page and upload it again.")
    _sync(journey)
    if sweep:
        assert _sweep(journey)["pages"] == 1
    [row] = _sql(journey, "SELECT task_id FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k",
                 k=f"upload-quality:{journey.journey_id}:{batch_id}")
    assert _task(journey, batch_id, "upload-quality")["task_status"] == "READY"
    return batch_id, rows, row["task_id"]


def _as(journey, subject):
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=subject)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    return TestClient(app, raise_server_exceptions=False)


def _act(journey, task_id, comment="__missing__", *, subject=None, action=CLOSE):
    body = {"action": action} if comment == "__missing__" else {"action": action, "comment": comment}
    return _as(journey, subject or journey.actor_id).post(
        f"/p2/v1/tenants/{journey.tenant_id}/tasks/{task_id}/actions", json=body)


def _events(journey, task_id):
    return _sql(journey, "SELECT event_type, actor_id, actor_role_code, comment, details, created_at_utc "
                         "FROM auditcore.p2_task_events WHERE tenant_id=:t AND task_id=:i ORDER BY task_event_id",
                i=task_id)


def _status(journey, task_id):
    [row] = _sql(journey, "SELECT task_status, completion_result FROM auditcore.p2_tasks "
                          "WHERE tenant_id=:t AND task_id=:i", i=task_id)
    return row


@pytest.mark.parametrize("swept_first", [False, True])
def test_closing_the_task_by_hand_records_the_comment_who_and_when(journey, swept_first):
    batch_id, rows, task_id = _quality_task(journey, sweep=swept_first)
    assert _gate(journey) == ["PC_PAGE_QUALITY_REJECTED"]

    response = _act(journey, task_id, f"  {REASON}  ")  # any non-blank comment closes it
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "VERIFIED_COMPLETE"

    task = _status(journey, task_id)
    result = task["completion_result"]
    assert task["task_status"] == "VERIFIED_COMPLETE"
    assert result["outcome"] == "CLOSED_BY_PC" and result["reason"] == REASON
    assert result["closedBy"] == journey.actor_id and result["closedRole"] == "PC" and result["closedAt"]
    [event] = [e for e in _events(journey, task_id) if e["event_type"] == CLOSE]
    assert (event["actor_id"], event["actor_role_code"], event["comment"]) == (journey.actor_id, "PC", REASON)
    assert event["created_at_utc"] is not None

    # The History the web card reads carries it, with the reason.
    detail = _as(journey, journey.actor_id).get(f"/p2/v1/tenants/{journey.tenant_id}/tasks/{task_id}").json()
    shown = [e for e in detail["events"] if e["event_type"] == CLOSE]
    assert [(e["actor_role_code"], e["comment"]) for e in shown] == [("PC", REASON)]

    # The task is out of the delivery gate; nothing else about the file moves.
    assert _gate(journey) == []
    _sync(journey)
    _sweep(journey)
    _sync(journey)
    assert _status(journey, task_id)["task_status"] == "VERIFIED_COMPLETE"  # not reopened by a sync or the sweep
    page = _page(journey, rows[1]["queue_id"])
    assert page["last_error"] == BLUR  # still the refused page, with its code and reason
    assert page["queue_status"] == "CANCELLED"  # off the Journey by the sweep, as without the close
    assert _batch_status(journey, batch_id) == "PARTIAL_FAILURE"
    assert len(_sql(journey, "SELECT 1 FROM auditcore.p2_activity_events WHERE event_type='PAGE_SWEPT'")) == 1
    assert _gate(journey) == []


def test_closing_before_the_sweep_leaves_the_page_failed_until_the_sweep(journey):
    _, rows, task_id = _quality_task(journey)
    assert _act(journey, task_id, REASON).status_code == 200
    assert _page(journey, rows[1]["queue_id"])["queue_status"] == "FAILED"
    assert _sweep(journey)["pages"] == 1  # the clean-up still takes it off the Journey
    assert _status(journey, task_id)["task_status"] == "VERIFIED_COMPLETE"


@pytest.mark.parametrize("reason", ["__missing__", None, "", "   ", "\n\t "])
def test_a_blank_comment_is_refused_by_the_server(journey, reason):
    _, _, task_id = _quality_task(journey)
    response = _act(journey, task_id, reason)
    assert response.status_code == 422, response.text
    assert "comment" in response.json()["detail"].lower()
    assert _status(journey, task_id)["task_status"] == "READY"
    assert [e for e in _events(journey, task_id) if e["event_type"] == CLOSE] == []
    assert _gate(journey) == ["PC_PAGE_QUALITY_REJECTED"]


def test_only_whoever_the_task_is_assigned_to_can_close_it(journey):
    _, _, task_id = _quality_task(journey)
    tl = _tl(journey)
    refused = _act(journey, task_id, REASON, subject=tl)
    assert refused.status_code == 403, refused.text
    assert _status(journey, task_id)["task_status"] == "READY"
    assert [e for e in _events(journey, task_id) if e["event_type"] == CLOSE] == []
    # A task already closed is closed: a second attempt is a conflict, not a second event.
    assert _act(journey, task_id, REASON).status_code == 200
    assert _act(journey, task_id, "again").status_code == 409
    assert len([e for e in _events(journey, task_id) if e["event_type"] == CLOSE]) == 1


def test_the_action_belongs_to_the_image_quality_task_only(journey):
    from audit_core.uc03_p2_registry import get_registry

    tasks = get_registry().tasks
    assert [name for name, template in tasks.items() if CLOSE in template.actions] == ["PC_PAGE_QUALITY_REJECTED"]
    # The file's other failed page keeps its own, unchanged task and cannot use it.
    batch_id, rows = _file(journey, [("booking_form", "READY"), (None, "FAILED"), (None, "FAILED")])
    _fail(journey, rows[1]["queue_id"], BLUR)
    _fail(journey, rows[2]["queue_id"])
    _sync(journey)
    resubmit = _task(journey, batch_id)
    assert resubmit["task_type"] == "DOCUMENT_RESUBMIT" and resubmit["task_status"] == "READY"
    [row] = _sql(journey, "SELECT task_id FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k",
                 k=f"resubmit:{journey.journey_id}:{batch_id}")
    assert _act(journey, row["task_id"], REASON).status_code == 422
    assert _status(journey, row["task_id"])["task_status"] == "READY"


def test_an_open_quality_task_raised_before_the_action_existed_gets_it(journey):
    _, _, task_id = _quality_task(journey)
    _sql(journey, "UPDATE auditcore.p2_tasks SET allowed_actions='[\"REUPLOAD_DOCUMENT\",\"ADD_COMMENT\"]'::jsonb "
                  "WHERE task_id=:i", i=task_id)
    assert _act(journey, task_id, REASON).status_code == 422
    _sync(journey)
    [row] = _sql(journey, "SELECT allowed_actions FROM auditcore.p2_tasks WHERE task_id=:i", i=task_id)
    assert row["allowed_actions"] == ["REUPLOAD_DOCUMENT", CLOSE, "ADD_COMMENT"]
    assert _act(journey, task_id, REASON).status_code == 200
