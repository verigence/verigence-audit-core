"""Files that failed on our side are uploaded again from what we stored (product owner 2026-11-02).

A page not read within its 30 minutes and swept from the booking, or a file
never split into pages, kept asking the PC to upload the file again although
the file was in storage. OUR_SIDE_REUPLOAD does that for the PC: the stored
original becomes a new upload of the booking (per outlet, per day, under the
customer's name), split like any upload. Real Postgres and the real split."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
import test_p2_dayend_sweep as sw
from conftest import delete_tenant_data
from p2_support import MemoryStorage, create_p2_journey, database_engine

from audit_core import uc03_p2_our_side_reupload as job
from audit_core import uc03_p2_storage
from audit_core import uc03_p2_worker as worker
from audit_core.uc03_p2_storage import P2DocumentStorageError
from audit_core.uc03_p2_task_producer import FILE_NOT_UPLOADED_IN_TIME_REASON

NOT_READ = "Not read within 30 minutes. Please upload this page again."


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
    created = create_p2_journey(engine, prefix="p2ours")
    created.storage = storage  # type: ignore[attr-defined]
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _run(journey, label: str, refs: list[str] | None = None) -> dict:
    return job.run_our_side_reupload(journey.engine, f"{journey.tenant_id}:{label}", refs)


def _batches(journey) -> list[dict]:
    return sw._sql(journey, "SELECT batch_id, original_filename, batch_status, failure_reason, original_object_key, "
                            "uploaded_by_actor_id, client_upload_id FROM auditcore.p2_upload_batches "
                            "WHERE tenant_id=:t ORDER BY created_at_utc")


def _swept_page_file(journey) -> tuple:
    """A two-page file: page 1 read, page 2 not read within its 30 minutes and
    swept at 02:00, its "Document to resubmit" task open."""
    payload = sw._pdf(200, 300)
    batch_id = sw._upload(journey, journey.storage, "Malaya kumar Mahanta.pdf", payload)
    sw._split(journey, batch_id)
    ids = sw._sql(journey, "SELECT queue_id FROM auditcore.p2_document_queue WHERE batch_id=:b ORDER BY page_number",
                  b=batch_id)
    sw._sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='READY' WHERE queue_id=:q",
            q=ids[0]["queue_id"])
    sw._fail(journey, ids[1]["queue_id"], code="NOT_READ_IN_30_MIN", reason=NOT_READ)
    with journey.engine.begin() as connection:
        sw.set_tenant_context(connection, journey.tenant_id)
        worker._refresh_batch_status(connection, journey.tenant_id, batch_id)
    sw._sync(journey)
    assert sw._sweep(journey)["pages"] == 1
    assert sw._task(journey, batch_id)["task_status"] == "READY"
    return batch_id, ids


def _never_split_file(journey, *, stored: bytes | None, size: int | None = None):
    batch_id = uuid4()
    key = f"p2/{batch_id}/original"
    payload = sw._pdf(200, 300, 400)
    if stored is not None:
        journey.storage.objects[key] = stored
    sw._sql(journey, "INSERT INTO auditcore.p2_upload_batches (tenant_id, batch_id, journey_id, original_filename, "
                     "content_type, size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id, "
                     "failure_reason, created_at_utc) VALUES (:t, :b, :j, 'Basant khilar file.pdf', 'application/pdf', "
                     ":n, 0, :k, 'FAILED', :a, :why, now() - interval '3 days')",
            b=batch_id, n=size if size is not None else len(payload), k=key, a=journey.actor_id,
            why=FILE_NOT_UPLOADED_IN_TIME_REASON)
    sw._sync(journey)
    assert sw._task(journey, batch_id)["title"] == "Basant khilar file.pdf: upload the file again"
    return batch_id, payload


def test_a_swept_page_s_file_is_listed_then_uploaded_again_and_only_the_unread_page_is_read(journey):
    first, ids = _swept_page_file(journey)
    dry = _run(journey, "ours-2026-11-02")
    [item] = dry["items"]
    assert (item["outcome"], item["file"], item["pagesFailed"], item["neverSplit"]) == (
        "WOULD_SEND", "Malaya kumar Mahanta.pdf", 1, False)
    assert len(_batches(journey)) == 1  # a dry run changes nothing

    done = _run(journey, "ours-2026-11-02-commit")
    [item] = done["items"]
    assert item["outcome"] == "SENT" and done["sent"] == 1
    new = next(b for b in _batches(journey) if str(b["batch_id"]) == item["newBatchId"])
    # Kept per outlet and day, under the customer's name; the PC's upload.
    assert new["original_object_key"].startswith(f"p2-documents/{journey.tenant_id}/O/")
    assert new["original_object_key"].endswith("/original/Malaya_kumar_Mahanta__P2-Customer__pc.pdf")
    assert journey.storage.objects[new["original_object_key"]] == journey.storage.objects[f"p2/{first}/original"]
    assert (new["batch_status"], new["uploaded_by_actor_id"]) == ("UPLOADED", journey.actor_id)
    assert sw._sql(journey, "SELECT work_status FROM auditcore.p2_work_queue WHERE tenant_id=:t "
                            "AND work_type='SPLIT_BATCH' AND work_key=:k", k=item["newBatchId"]) == [
        {"work_status": "PENDING"}]

    # The split does what it does for a file uploaded again.
    sw._split(journey, new["batch_id"])
    pages = sw._sql(journey, "SELECT page_number, queue_status, status_reason FROM auditcore.p2_document_queue "
                             "WHERE batch_id=:b ORDER BY page_number", b=new["batch_id"])
    assert pages[0]["queue_status"] == "CANCELLED" and pages[0]["status_reason"].startswith("Same as page 1")
    assert pages[1]["queue_status"] == "QUEUED", "the page never read is read now"
    old = sw._page(journey, ids[1]["queue_id"])
    assert (old["queue_status"], old["last_error"]) == ("CANCELLED", "SUPERSEDED_PAGE")
    assert sw._task(journey, first)["task_status"] == "VERIFIED_COMPLETE"
    # Nothing is left to send.
    assert _run(journey, "ours-2026-11-02-commit")["files"] == 0


def test_a_file_never_split_is_sent_again_when_stored_whole_and_its_task_closes(journey):
    batch_id, payload = _never_split_file(journey, stored=None)
    batch_id_ok = batch_id
    journey.storage.objects[f"p2/{batch_id}/original"] = payload
    done = _run(journey, "ours-commit")
    [item] = done["items"]
    assert item["outcome"] == "SENT" and item["neverSplit"]
    by_id = {str(b["batch_id"]): b for b in _batches(journey)}
    old, new = by_id[str(batch_id_ok)], by_id[item["newBatchId"]]
    assert old["batch_status"] == "CANCELLED"
    assert old["failure_reason"].startswith("Sent again by Verigence from the stored file on ")
    assert sw._task(journey, batch_id)["task_status"] in ("VERIFIED_COMPLETE", "CANCELLED")
    assert new["batch_status"] == "UPLOADED" and new["client_upload_id"] == f"our-side-{batch_id}"
    sw._split(journey, new["batch_id"])
    assert len(sw._sql(journey, "SELECT 1 FROM auditcore.p2_document_queue WHERE batch_id=:b AND "
                                "queue_status='QUEUED'", b=new["batch_id"])) == 3
    assert _run(journey, "ours-commit")["files"] == 0


@pytest.mark.parametrize("stored", ["missing", "short"])
def test_a_file_that_never_reached_storage_whole_stays_with_the_pc(journey, stored):
    payload = sw._pdf(200, 300, 400)
    batch_id, _ = _never_split_file(journey, stored=None if stored == "missing" else payload[:100], size=len(payload))
    done = _run(journey, "ours-commit")
    [item] = done["items"]
    assert item["outcome"] == "NOT_IN_STORAGE" and "the PC has to upload it again" in item["why"]
    assert [b["batch_status"] for b in _batches(journey)] == ["FAILED"]
    assert sw._task(journey, batch_id)["task_status"] == "READY"


def test_what_the_pc_settled_a_reviewed_booking_and_a_bad_scan_are_left_alone(journey):
    first, ids = _swept_page_file(journey)
    # The PC closed the file's task by hand.
    sw._sql(journey, "UPDATE auditcore.p2_tasks SET task_status='VERIFIED_COMPLETE', "
                     "completion_result=CAST(:r AS jsonb) WHERE tenant_id=:t AND dedupe_key=:k",
            r=json.dumps({"outcome": "CLOSED_BY_PC"}), k=f"resubmit:{journey.journey_id}:{first}")
    [item] = _run(journey, "ours")["items"]
    assert item["outcome"] == "SKIPPED" and item["why"].startswith("the PC closed its task")
    sw._sql(journey, "UPDATE auditcore.p2_tasks SET completion_result='{}'::jsonb WHERE tenant_id=:t "
                     "AND dedupe_key=:k", k=f"resubmit:{journey.journey_id}:{first}")
    # The PC uploaded the file again since.
    again = sw._upload(journey, journey.storage, "Malaya kumar Mahanta.pdf", sw._pdf(250))
    sw._sql(journey, "UPDATE auditcore.p2_upload_batches SET batch_status='COMPLETED' WHERE batch_id=:b", b=again)
    [item] = [i for i in _run(journey, "ours")["items"] if i["batchId"] == str(first)]
    assert item["outcome"] == "SKIPPED" and item["why"] == "the PC uploaded it again (Malaya kumar Mahanta.pdf)"
    sw._sql(journey, "UPDATE auditcore.p2_upload_batches SET original_filename='other.pdf', size_bytes=1 "
                     "WHERE batch_id=:b", b=again)
    # A booking whose documents are marked reviewed.
    sw._sql(journey, "INSERT INTO auditcore.p2_stage_document_reviews (tenant_id, journey_id, stage_code, "
                     "reviewed_by_actor_id, reviewed_by_role_code, document_count, dealer_id, outlet_id) "
                     "SELECT tenant_id, journey_id, 'BOOKING', :a, 'TL', 1, dealer_id, outlet_id FROM auditcore.journeys "
                     "WHERE tenant_id=:t AND journey_id=:j", a=journey.actor_id)
    [item] = _run(journey, "ours")["items"]
    assert item["outcome"] == "SKIPPED" and item["why"] == "the booking's documents are marked reviewed"
    sw._sql(journey, "DELETE FROM auditcore.p2_stage_document_reviews WHERE tenant_id=:t")
    # A page refused for scan quality is the page's own problem: not listed.
    sw._sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='DI_QUALITY_IMAGE_BLUR_SCORE' WHERE queue_id=:q",
            q=ids[1]["queue_id"])
    assert _run(journey, "ours")["files"] == 0
    # Another customer's file taken out of the booking: not listed either.
    sw._sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='NOT_READ_IN_30_MIN' WHERE queue_id=:q",
            q=ids[1]["queue_id"])
    sw._sql(journey, "UPDATE auditcore.p2_upload_batches SET identity_status='FOREIGN', identity_enforced_at_utc=now() "
                     "WHERE batch_id=:b", b=first)
    assert _run(journey, "ours")["files"] == 0


def test_only_the_listed_bookings_and_at_most_the_per_run_number(journey, monkeypatch):
    _swept_page_file(journey)
    assert _run(journey, "ours", ["NOT-THIS-ONE"])["files"] == 0
    monkeypatch.setattr(job, "_PER_RUN", 0)
    done = _run(journey, "ours-commit")
    assert [i["outcome"] for i in done["items"]] == ["NEXT_RUN"] and done["left_for_next_run"] == 1
    assert len(_batches(journey)) == 1


def test_the_job_row_keeps_what_the_job_returned(journey, monkeypatch):
    _swept_page_file(journey)
    job_row = {"job_id": uuid4(), "job_kind": "OUR_SIDE_REUPLOAD", "tag": f"{journey.tenant_id}:ours", "reports": []}
    with journey.engine.begin() as connection:
        sw.set_tenant_context(connection, journey.tenant_id)
        connection.execute(sw.text("SELECT set_config('app.platform_super_admin','true',true)"))
        connection.execute(sw.text("INSERT INTO auditcore.p2_worker_jobs (job_id, job_kind, tag, reports, status, "
                                   "requested_by) VALUES (:i, :k, :g, '{}', 'RUNNING', 'test')"),
                           {"i": job_row["job_id"], "k": job_row["job_kind"], "g": job_row["tag"]})
    try:
        worker._run_job(journey.engine, job_row)
        with journey.engine.begin() as connection:
            connection.execute(sw.text("SELECT set_config('app.platform_super_admin','true',true)"))
            status, detail = connection.execute(
                sw.text("SELECT status, detail FROM auditcore.p2_worker_jobs WHERE job_id=:i"),
                {"i": job_row["job_id"]}).one()
        assert status == "DONE"
        result = json.loads(detail)
        assert result["files"] == 1 and result["items"][0]["outcome"] == "WOULD_SEND"
    finally:
        with journey.engine.begin() as connection:
            connection.execute(sw.text("SELECT set_config('app.platform_super_admin','true',true)"))
            connection.execute(sw.text("DELETE FROM auditcore.p2_worker_jobs WHERE job_id=:i"), {"i": job_row["job_id"]})


def test_the_customer_s_name_in_the_key_is_the_kyc_name_else_the_entered_one_never_a_placeholder(journey):
    from audit_core.uc03_p2_object_keys import upload_labels

    def labels():
        with journey.engine.begin() as connection:
            sw.set_tenant_context(connection, journey.tenant_id)
            return upload_labels(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)

    customer = "SELECT customer_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"
    assert labels()[2] == "P2 Customer"  # the name the PC entered
    sw._sql(journey, f"UPDATE auditcore.customers SET legal_name='MALAYA KUMAR MAHANTA' WHERE tenant_id=:t "
                     f"AND customer_id=({customer})")
    assert labels()[2] == "MALAYA KUMAR MAHANTA"  # the KYC name once on file
    # A booking started from an upload: its entered name is the journey's id, a placeholder.
    # (The entered name never changes: a booking with that placeholder is made here.)
    sw._sql(journey, "WITH placeholder AS (INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, "
                     "customer_type_code, display_name) SELECT tenant_id, dealer_id, outlet_id, 'INDIVIDUAL', "
                     "CAST(journey_id AS text) FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j "
                     "RETURNING customer_id) "
                     "UPDATE auditcore.journeys SET customer_id=(SELECT customer_id FROM placeholder) "
                     "WHERE tenant_id=:t AND journey_id=:j")
    assert labels()[2] is None
