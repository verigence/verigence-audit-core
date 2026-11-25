"""Every document that failed on our side is sent again by itself, once (product owner 22 Nov 2026).

"What happens with the failed journey uploads.. are they being reloaded or they silently got
missed out" -- they waited for the PC. Then: "pls send 48 AI refused pages from storage and
create one job at 8:15 am which handles all the failed transactions".

Real Postgres, the real split and the real "Send again"."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import test_p2_dayend_sweep as sw
from conftest import delete_tenant_data
from p2_support import MemoryStorage, create_p2_journey, database_engine

from audit_core import job_schedule, uc03_p2_storage
from audit_core import uc03_p2_failed_resend as job
from audit_core import uc03_p2_our_side_reupload as reupload
from audit_core import uc03_p2_worker as worker
from audit_core.uc03_p2_api import _send_page_again
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_storage import P2DocumentStorageError

NOT_READ = "Not read within 30 minutes. Please upload this page again."
AI_REFUSED = "AI reading failed — tried 3 times. Please upload this page again."
BLURRED = "Page rejected: the page is blurred. Re-scan this page and upload it again."


class _Storage(MemoryStorage):
    def head_object(self, key: str) -> dict:
        if key not in self.objects:
            raise P2DocumentStorageError(f"no {key}")
        return {"contentLength": len(self.objects[key])}


@pytest.fixture
def journey(monkeypatch):
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    storage = _Storage()
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(uc03_p2_storage, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(worker, "_owned", lambda *args, **kwargs: None)
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2again")
    created.storage = storage  # type: ignore[attr-defined]
    try:
        yield created
    finally:
        with engine.begin() as connection:
            connection.execute(sw.text("SELECT set_config('app.platform_super_admin','true',true)"))
            connection.execute(sw.text("DELETE FROM auditcore.p2_worker_jobs WHERE tag LIKE :p"),
                               {"p": f"{created.tenant_id}:%"})
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _run(journey, label: str) -> dict:
    return job.run_failed_resend(journey.engine, f"{journey.tenant_id}:{label}")


def _unit(journey, queue_id) -> dict:
    [row] = sw._sql(journey, "SELECT * FROM auditcore.p2_document_queue WHERE queue_id=:q", q=queue_id)
    return row


def _file(journey, name: str = "Delivery file.pdf") -> tuple:
    """A two-page file: page 1 read, page 2 still to be dealt with."""
    batch_id = sw._upload(journey, journey.storage, name, sw._pdf(220, 320))
    sw._split(journey, batch_id)
    ids = [r["queue_id"] for r in sw._sql(
        journey, "SELECT queue_id FROM auditcore.p2_document_queue WHERE batch_id=:b ORDER BY page_number", b=batch_id)]
    sw._sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='READY' WHERE queue_id=:q", q=ids[0])
    return batch_id, ids


def _fail_and_sweep(journey, batch_id, queue_id, code: str, reason: str) -> None:
    sw._fail(journey, queue_id, code=code, reason=reason)
    with journey.engine.begin() as connection:
        sw.set_tenant_context(connection, journey.tenant_id)
        worker._refresh_batch_status(connection, journey.tenant_id, batch_id)
    sw._sync(journey)
    assert sw._sweep(journey)["pages"] == 1


def _pc_sends_again(journey, queue_id, *, template=None, quality_confirmed: bool = False) -> UUID:
    """What "Send again" / "Set type and send" on the PC's task does."""
    unit = _unit(journey, queue_id)
    with journey.engine.begin() as connection:
        sw.set_tenant_context(connection, journey.tenant_id)
        return _send_page_again(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, unit=unit, template=template,
            di_type=template.di_types[0] if template is not None else None, actor_id=journey.actor_id, role="PC",
            swept=True, event_type="PAGE_SENT_AGAIN", override_reason=None, correlation_id=None,
            quality_confirmed=quality_confirmed)


def _a_document_type():
    return next(t for t in get_registry().documents.values() if t.di_types and t.key != "supporting_document")


def _pc_made_document_we_did_not_read(journey, *, template=None, quality_confirmed=False,
                                      code="NOT_READ_IN_30_MIN", reason=NOT_READ) -> tuple:
    """18 Nov, live: the scan-quality check refused a page, the PC looked at it and pressed
    "Send again", and we did not read it within its 30 minutes."""
    batch_id, ids = _file(journey)
    _fail_and_sweep(journey, batch_id, ids[1], "DI_QUALITY_IMAGE_BLUR_SCORE", BLURRED)
    document = _pc_sends_again(journey, ids[1], template=template, quality_confirmed=quality_confirmed)
    _fail_and_sweep(journey, batch_id, document, code, reason)
    return batch_id, document


# ── 1. what a person made is read again as that person left it ─────────────────────────────
def test_a_scan_the_pc_confirmed_is_sent_again_still_confirmed_and_the_file_is_not_uploaded_again(journey):
    batch_id, document = _pc_made_document_we_did_not_read(journey, quality_confirmed=True)

    dry = _run(journey, "morning")
    assert (dry["documents"]["would_send"], dry["files"]["files"]) == (1, 0)
    assert dry["documents"]["items"][0]["scanConfirmedByPc"] is True
    assert _unit(journey, document)["last_error"] == "NOT_READ_IN_30_MIN"   # a dry run changes nothing

    done = _run(journey, "morning-commit")
    [item] = done["documents"]["items"]
    assert item["outcome"] == "SENT" and done["files"]["files"] == 0
    new = _unit(journey, item["newQueueId"])
    assert (new["queue_status"], new["unit_kind"], new["group_source"]) == ("QUEUED", "GROUP", "PC")
    assert new["quality_confirmed"] is True, "the PC's word that the scan is good was lost"
    assert list(new["page_numbers"]) == [2] and new["batch_id"] == batch_id
    old = _unit(journey, document)
    assert (old["queue_status"], old["last_error"]) == ("CANCELLED", "SUPERSEDED_PAGE")
    assert sw._sql(journey, "SELECT work_status FROM auditcore.p2_work_queue WHERE tenant_id=:t "
                            "AND work_type='DOCUMENT_INGEST' AND work_key=:k", k=item["newQueueId"]) == [
        {"work_status": "PENDING"}]
    assert len(sw._sql(journey, "SELECT 1 AS x FROM auditcore.p2_upload_batches WHERE tenant_id=:t")) == 1


def test_a_document_the_pc_typed_is_sent_again_as_that_type(journey):
    template = _a_document_type()
    _pc_made_document_we_did_not_read(journey, template=template)
    done = _run(journey, "morning-commit")
    new = _unit(journey, done["documents"]["items"][0]["newQueueId"])
    assert new["template_key"] == template.key and new["classified_document_type"] == template.di_types[0]


def test_an_ai_answer_refused_on_a_pc_made_document_is_sent_again_too(journey):
    """The 48 pages of 22 Nov: most were documents a PC had typed or sent again."""
    _pc_made_document_we_did_not_read(journey, template=_a_document_type(),
                                      code="DOCUMENT_AI_RESPONSE_INVALID", reason=AI_REFUSED)
    assert _run(journey, "morning-commit")["documents"]["sent"] == 1


# ── 2. once, and only once ─────────────────────────────────────────────────────────────────
def test_a_document_sent_again_that_fails_again_stays_with_the_pc(journey):
    batch_id, _document = _pc_made_document_we_did_not_read(journey, quality_confirmed=True)
    first = _run(journey, "morning-commit")["documents"]["items"][0]["newQueueId"]
    assert _run(journey, "next-morning-commit")["documents"]["documents"] == 0   # being read: nothing to do

    _fail_and_sweep(journey, batch_id, first, "NOT_READ_IN_30_MIN", NOT_READ)
    again = _run(journey, "third-morning-commit")
    [item] = again["documents"]["items"]
    assert item["outcome"] == "SKIPPED" and item["why"].startswith("it was sent again once already")
    assert again["files"]["files"] == 0, "the whole file was uploaded again for a document already tried twice"
    assert _unit(journey, first)["last_error"] == "NOT_READ_IN_30_MIN"
    assert sw._task(journey, batch_id)["task_status"] == "READY"   # the PC's task is still there


def test_a_reviewed_booking_is_left_alone(journey):
    _pc_made_document_we_did_not_read(journey, quality_confirmed=True)
    sw._sql(journey, "INSERT INTO auditcore.p2_stage_document_reviews (tenant_id, journey_id, stage_code, "
                     "reviewed_by_actor_id, reviewed_by_role_code, document_count, dealer_id, outlet_id) "
                     "SELECT tenant_id, journey_id, 'BOOKING', :a, 'TL', 1, dealer_id, outlet_id FROM auditcore.journeys "
                     "WHERE tenant_id=:t AND journey_id=:j", a=journey.actor_id)
    [item] = _run(journey, "morning-commit")["documents"]["items"]
    assert (item["outcome"], item["why"]) == ("SKIPPED", "the booking's documents are marked reviewed")


def test_a_pc_made_document_refused_for_several_documents_on_its_page_is_not_sent_again(journey):
    _pc_made_document_we_did_not_read(journey, template=_a_document_type(), code="PAGE_HAS_SEVERAL_DOCUMENTS",
                                      reason=worker.SEVERAL_DOCUMENTS_REASON)
    done = _run(journey, "morning-commit")
    assert (done["documents"]["documents"], done["files"]["files"]) == (0, 0)


def test_at_most_so_many_in_one_run(journey, monkeypatch):
    _pc_made_document_we_did_not_read(journey, quality_confirmed=True)
    monkeypatch.setattr(job, "_DOCUMENTS", 0)
    done = _run(journey, "morning-commit")
    assert done["documents"]["left_for_next_run"] == 1 and done["documents"]["sent"] == 0


# ── 3. every other failure: the whole file again ───────────────────────────────────────────
@pytest.mark.parametrize("code, reason", [
    ("NOT_READ_IN_30_MIN", NOT_READ),
    ("DOCUMENT_AI_RESPONSE_INVALID", AI_REFUSED),
    ("DiClientError (at audit_core/di_client.py:212)", "A system problem stopped this page — tried 3 times."),
    ("GROUPING_FAILED", "The pages of this document could not be put together and read."),
    # The AI service could not take it after its retries (25 Nov 2026: the 24 Nov outage).
    ("DOCUMENT_AI_UNAVAILABLE", "AI reading failed — tried 3 times. Please upload this page again."),
    ("DOCUMENT_AI_RATE_LIMITED", "AI reading failed — tried 3 times. Please upload this page again."),
])
def test_a_page_never_read_has_its_file_uploaded_again(journey, code, reason):
    batch_id, ids = _file(journey, "Booking file.pdf")
    _fail_and_sweep(journey, batch_id, ids[1], code, reason)
    dry = _run(journey, "morning")
    assert (dry["documents"]["documents"], dry["files"]["would_send"]) == (0, 1)
    done = _run(journey, "morning-commit")
    assert done["files"]["sent"] == 1
    new_file = done["files"]["items"][0]["newBatchId"]
    sw._split(journey, new_file)
    pages = sw._sql(journey, "SELECT page_number, queue_status FROM auditcore.p2_document_queue "
                             "WHERE batch_id=:b ORDER BY page_number", b=new_file)
    assert [p["queue_status"] for p in pages] == ["CANCELLED", "QUEUED"]   # page 1 was read; page 2 is read now
    assert _unit(journey, ids[1])["last_error"] == "SUPERSEDED_PAGE"
    assert _run(journey, "next-morning-commit")["files"]["files"] == 0      # once


@pytest.mark.parametrize("code", ["DI_QUALITY_IMAGE_BLUR_SCORE", "DOCUMENT_AI_CONTENT_BLOCKED", "DUPLICATE_PAGE",
                                  # more than one document on the page (22 Nov 2026)
                                  "PAGE_HAS_SEVERAL_DOCUMENTS"])
def test_a_page_refused_for_what_it_is_stays_with_the_pc(journey, code):
    batch_id, ids = _file(journey)
    _fail_and_sweep(journey, batch_id, ids[1], code, "Refused.")
    done = _run(journey, "morning-commit")
    assert (done["documents"]["documents"], done["files"]["files"]) == (0, 0)


def test_the_file_upload_job_alone_no_longer_uploads_a_file_for_a_pc_made_document(journey):
    """22 Nov, live: OUR_SIDE_REUPLOAD uploaded whole files again for documents a PC had sent
    again as good scans; read as fresh pages they were refused for scan quality once more."""
    _pc_made_document_we_did_not_read(journey, quality_confirmed=True)
    assert reupload.run_our_side_reupload(journey.engine, f"{journey.tenant_id}:ours")["files"] == 0


# ── 4. every morning at 8:15, by itself ────────────────────────────────────────────────────
def test_the_job_is_on_the_jobs_page_at_a_quarter_past_eight_and_its_time_can_be_changed():
    entry = job_schedule.DAILY_JOBS["FAILED_RESEND"]
    assert (entry.default_ist, entry.run_name, entry.can_be_off) == ("08:15", job.RUN_NAME, True)
    before = job_schedule.time_ist("FAILED_RESEND")
    try:
        job_schedule.apply({"FAILED_RESEND": "08:15"})
        assert worker._FAILED_RESEND_UTC == "02:45"
        day = datetime(2026, 11, 23, tzinfo=UTC)
        assert worker._failed_resend_due("tenant-y", day.replace(hour=2, minute=44)) is None
        assert worker._failed_resend_due("tenant-y", day.replace(hour=2, minute=45)) == "2026-11-23"
        job_schedule.apply({"FAILED_RESEND": "09:00"})
        assert worker._FAILED_RESEND_UTC == "03:30"
    finally:
        job_schedule.apply({"FAILED_RESEND": before})


def _jobs(journey) -> list[dict]:
    with journey.engine.begin() as connection:
        connection.execute(sw.text("SELECT set_config('app.platform_super_admin','true',true)"))
        return [dict(r) for r in connection.execute(
            sw.text("SELECT job_id, job_kind, tag, status, requested_by, detail FROM auditcore.p2_worker_jobs "
                    "WHERE tag LIKE :p ORDER BY requested_at_utc"), {"p": f"{journey.tenant_id}:%"}).mappings().all()]


def test_the_morning_run_is_asked_for_once_a_day_and_sends_without_a_dry_run(journey, monkeypatch):
    _batch, document = _pc_made_document_we_did_not_read(journey, quality_confirmed=True)
    monkeypatch.setattr(worker, "_FAILED_RESEND_UTC", "02:45")
    monkeypatch.setattr(worker, "_last_failed_resend", {})
    morning = datetime(2026, 11, 23, 2, 46, tzinfo=UTC)

    assert worker.run_failed_resend_daily(journey.engine, journey.tenant_id, morning.replace(minute=40)) is None
    assert worker.run_failed_resend_daily(journey.engine, journey.tenant_id, morning) is True
    [asked] = _jobs(journey)
    assert (asked["job_kind"], asked["tag"], asked["status"], asked["requested_by"]) == (
        "FAILED_RESEND", f"{journey.tenant_id}:daily-2026-11-23-commit", "PENDING", "system")
    # the other worker, a restart, the next pass: the day is not asked for twice
    monkeypatch.setattr(worker, "_last_failed_resend", {})
    assert worker.run_failed_resend_daily(journey.engine, journey.tenant_id, morning) is False
    assert len(_jobs(journey)) == 1
    assert sw._sql(journey, "SELECT run_key FROM auditcore.p2_scheduled_runs WHERE tenant_id=:t AND run_name=:n",
                   n=job.RUN_NAME) == [{"run_key": "2026-11-23"}]          # what the Jobs page shows as last run

    # The worker runs it: no dry run is asked of the morning's own run.
    with journey.engine.begin() as connection:
        connection.execute(sw.text("SELECT set_config('app.platform_super_admin','true',true)"))
        assert worker.dry_run_missing(connection, asked) is None
        connection.execute(sw.text("UPDATE auditcore.p2_worker_jobs SET status='RUNNING' WHERE job_id=:i"),
                           {"i": asked["job_id"]})
    worker._run_job(journey.engine, {**asked, "reports": []})
    [ran] = _jobs(journey)
    assert ran["status"] == "DONE", ran["detail"]
    result = json.loads(ran["detail"])
    assert result["commit"] is True and result["documents"]["sent"] == 1
    assert _unit(journey, document)["last_error"] == "SUPERSEDED_PAGE"


def test_a_run_a_person_asks_for_still_needs_its_dry_run(journey):
    asked = {"job_id": uuid4(), "job_kind": "FAILED_RESEND", "tag": f"{journey.tenant_id}:by-hand-commit"}
    with journey.engine.begin() as connection:
        connection.execute(sw.text("SELECT set_config('app.platform_super_admin','true',true)"))
        assert "no dry run" in (worker.dry_run_missing(connection, asked) or "")
        # and nobody passes as the morning's run with another kind of job
        other = {**asked, "job_kind": "RULES_RERUN", "tag": f"{journey.tenant_id}:daily-2026-11-23-commit"}
        assert "no dry run" in (worker.dry_run_missing(connection, other) or "")
