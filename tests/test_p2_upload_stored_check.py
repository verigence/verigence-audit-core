"""A file stored whole that the browser never confirmed starts processing (2026-11-02).

Live, 2 Nov 2026: 3 of the 13 files failed as "The file did not finish
uploading within 30 minutes" were in R2 at their full size -- the browser
finished the PUT and never called :finalize. Product owner: a file saved in
R2 must start the usual process. The worker looks a file still waiting for
its upload up in storage a few minutes after it was prepared; found whole, it
is accepted exactly as :finalize accepts it (uc03_p2_upload_accept) and split
like any upload. Real Postgres, the real API, the real split."""
from __future__ import annotations

import os

import pytest
import test_p2_dayend_sweep as sw
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    MemoryStorage,
    create_p2_journey,
    database_engine,
)

from audit_core import uc03_p2_api as api
from audit_core import uc03_p2_storage
from audit_core import uc03_p2_worker as worker
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_storage import P2DocumentStorageError
from audit_core.uc03_p2_task_producer import (
    FILE_NOT_SPLIT_IN_TIME_REASON,
    FILE_NOT_UPLOADED_IN_TIME_REASON,
)

BY_VERIGENCE = {"by": "Verigence", "how": "STORED_FILE_FOUND",
                "why": "The file was found stored whole; the browser never confirmed the upload."}


class _Storage(MemoryStorage):
    """R2 as the browser and the worker see it: HEAD of a key never stored fails."""

    def __init__(self) -> None:
        super().__init__()
        self.heads: list[str] = []
        self.on_head = None

    def presign_put(self, key, *, content_type, expires_seconds=900):
        return f"https://storage.test/{key}"

    def head_object(self, key: str) -> dict:
        self.heads.append(key)
        if self.on_head is not None:
            hook, self.on_head = self.on_head, None
            hook()
        if key not in self.objects:
            raise P2DocumentStorageError(f"no {key}")
        return {"contentLength": len(self.objects[key]), "contentType": "application/pdf", "etag": "e"}


@pytest.fixture
def journey(monkeypatch):
    storage = _Storage()
    monkeypatch.setattr(api, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(uc03_p2_storage, "get_p2_document_storage", lambda: storage)
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2stored")
    created.storage = storage  # type: ignore[attr-defined]
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        for key in [k for k in worker._stored_checked_at if k[0] == created.tenant_id]:
            del worker._stored_checked_at[key]
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _base(journey) -> str:
    return f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"


def _prepare(journey, payload: bytes, *, stored: bytes | None, minutes_ago: float,
             upload_id: str = "stored-0001") -> str:
    """uploads:init as the browser calls it; ``stored`` is what reached R2."""
    init = _client().post(f"{_base(journey)}/uploads:init", json={"files": [{
        "filename": "booking-packet.pdf", "contentType": "application/pdf", "sizeBytes": len(payload),
        "clientUploadId": upload_id}]})
    assert init.status_code == 200, init.text
    [upload] = init.json()["uploads"]
    assert upload["status"] == "AWAITING_UPLOAD"
    if stored is not None:
        journey.storage.objects[upload["uploadUrl"].split("https://storage.test/", 1)[1]] = stored
    _age(journey, upload["batchId"], minutes_ago)
    return upload["batchId"]


def _age(journey, batch_id: str, minutes: float) -> None:
    sw._sql(journey, "UPDATE auditcore.p2_upload_batches SET created_at_utc=now() - (:m * interval '1 minute') "
                     "WHERE tenant_id=:t AND batch_id=:b", b=batch_id, m=minutes)


def _finalize(journey, batch_id: str):
    return _client().post(f"{_base(journey)}/uploads/{batch_id}:finalize")


def _pass(journey) -> int:
    return worker.accept_stored_uploads(journey.engine, journey.tenant_id)


def _worker_pass(journey, monkeypatch) -> None:
    """The worker's per-Project pass as its loop runs it: the stored-file
    check, then the 30-minute ceiling (the other sweeps are not under test)."""
    monkeypatch.setattr(worker, "_fact_sweep", lambda engine, tenant_id: 0)
    monkeypatch.setattr(worker, "backfill_price_variance", lambda engine, tenant_id: 0)
    monkeypatch.setattr(worker, "run_dayend_sweep", lambda engine, tenant_id: None)
    monkeypatch.setattr(worker, "run_task_manager", lambda engine, tenant_id: None)
    monkeypatch.setattr(worker, "_NIGHTLY_REVIEW_UTC", "23:59")
    worker._last_sweep_at.pop(journey.tenant_id, None)
    worker._last_watchdog_at.pop(journey.tenant_id, None)
    worker._maybe_sweep(journey.engine, journey.tenant_id)


def _batch(journey, batch_id: str) -> dict:
    [row] = sw._sql(journey, "SELECT batch_status, failure_reason, uploaded_by_actor_id, page_count "
                             "FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND batch_id=:b", b=batch_id)
    return dict(row)


def _split_work(journey, batch_id: str) -> list[dict]:
    return [dict(r) for r in sw._sql(
        journey, "SELECT work_status, payload, requested_version FROM auditcore.p2_work_queue "
                 "WHERE tenant_id=:t AND work_type='SPLIT_BATCH' AND work_key=:b", b=batch_id)]


def _accepted(journey, batch_id: str) -> list[dict]:
    return [r["details"] for r in sw._sql(
        journey, "SELECT details FROM auditcore.p2_activity_events WHERE tenant_id=:t "
                 "AND event_type='UPLOAD_ACCEPTED' AND subject_id=:b ORDER BY event_id", b=batch_id)]


# ------------------------------------------------------------------- found whole: accepted

def test_a_file_stored_whole_but_never_confirmed_is_accepted_and_split(journey, monkeypatch):
    payload = sw._pdf(200, 300)
    batch_id = _prepare(journey, payload, stored=payload, minutes_ago=4)

    _worker_pass(journey, monkeypatch)

    # Accepted as :finalize accepts, and the 30-minute ceiling of the same pass
    # leaves it alone: it was prepared 4 minutes ago.
    batch = _batch(journey, batch_id)
    assert (batch["batch_status"], batch["failure_reason"]) == ("UPLOADED", None)
    assert batch["uploaded_by_actor_id"] == journey.actor_id  # still the PC's upload
    [work] = _split_work(journey, batch_id)
    assert work["work_status"] == "PENDING"
    assert work["payload"] == {"batchId": batch_id, "uploadedBy": journey.actor_id, "uploadedByRole": "PC"}
    assert _accepted(journey, batch_id) == [{"filename": "booking-packet.pdf", "sizeBytes": len(payload),
                                             **BY_VERIGENCE}]

    # The split runs like for any upload: its pages are the PC's.
    [item] = [w for w in worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
              if w.work_type == "SPLIT_BATCH"]
    worker._split_batch(journey.engine, item)
    worker._settle(journey.engine, item, worker._complete)
    batch = _batch(journey, batch_id)
    assert (batch["batch_status"], batch["page_count"]) == ("PROCESSING", 2)
    pages = sw._sql(journey, "SELECT queue_status FROM auditcore.p2_document_queue WHERE tenant_id=:t "
                             "AND batch_id=:b ORDER BY page_number", b=batch_id)
    assert [p["queue_status"] for p in pages] == ["QUEUED", "QUEUED"]
    ingests = sw._sql(journey, "SELECT payload FROM auditcore.p2_work_queue WHERE tenant_id=:t "
                               "AND work_type='DOCUMENT_INGEST'")
    assert len(ingests) == 2
    assert {(i["payload"]["uploadedBy"], i["payload"]["uploadedByRole"]) for i in ingests} == {
        (journey.actor_id, "PC")}


def test_finalize_after_the_worker_accepted_answers_with_the_current_status(journey):
    payload = sw._pdf(200)
    batch_id = _prepare(journey, payload, stored=payload, minutes_ago=4)
    assert _pass(journey) == 1

    late = _finalize(journey, batch_id)  # the browser confirms after all

    assert late.status_code == 202, late.text
    assert late.json() == {"batchId": batch_id, "status": "UPLOADED"}
    assert len(_split_work(journey, batch_id)) == 1
    assert len(_accepted(journey, batch_id)) == 1


def test_a_confirmed_upload_is_accepted_by_finalize_exactly_as_before(journey):
    payload = sw._pdf(200)
    batch_id = _prepare(journey, payload, stored=payload, minutes_ago=0)

    first = _finalize(journey, batch_id)
    again = _finalize(journey, batch_id)

    assert (first.status_code, first.json()) == (202, {"batchId": batch_id, "status": "UPLOADED"})
    assert (again.status_code, again.json()) == (202, {"batchId": batch_id, "status": "UPLOADED"})
    [work] = _split_work(journey, batch_id)
    assert set(work["payload"]) == {"batchId", "uploadedBy", "uploadedByRole"}
    assert work["payload"]["uploadedBy"] == journey.actor_id
    assert _accepted(journey, batch_id) == [{"filename": "booking-packet.pdf", "sizeBytes": len(payload)}]
    # Nothing is waiting any more: the worker looks nothing up.
    _age(journey, batch_id, 10)
    journey.storage.heads.clear()
    assert _pass(journey) == 0 and journey.storage.heads == []


def test_uploads_paused_do_not_stop_a_file_already_prepared_as_for_finalize(journey):
    payload = sw._pdf(200)
    batch_id = _prepare(journey, payload, stored=payload, minutes_ago=4)
    sw._sql(journey, "INSERT INTO auditcore.p2_upload_pause (tenant_id, paused, message, set_by_actor_id) "
                     "VALUES (:t, true, 'Maintenance', 'test-admin')")

    assert _pass(journey) == 1
    assert _batch(journey, batch_id)["batch_status"] == "UPLOADED"


# ------------------------------------------------------------------- not there whole: left to the ceiling

@pytest.mark.parametrize("stored", ["missing", "short"])
def test_a_file_not_stored_whole_is_left_waiting_and_still_fails_at_30_minutes(journey, monkeypatch, stored):
    payload = sw._pdf(200, 300)
    batch_id = _prepare(journey, payload, stored=None if stored == "missing" else payload[:-10], minutes_ago=4)

    assert _pass(journey) == 0
    assert _batch(journey, batch_id)["batch_status"] == "AWAITING_UPLOAD"
    assert _split_work(journey, batch_id) == [] and _accepted(journey, batch_id) == []
    assert len(journey.storage.heads) == 1

    # At the ceiling it is looked up once more (although looked up just now),
    # then failed as today.
    _age(journey, batch_id, 31)
    _worker_pass(journey, monkeypatch)
    assert len(journey.storage.heads) == 2
    batch = _batch(journey, batch_id)
    assert (batch["batch_status"], batch["failure_reason"]) == ("FAILED", FILE_NOT_UPLOADED_IN_TIME_REASON)
    assert _split_work(journey, batch_id) == [] and _accepted(journey, batch_id) == []


def test_a_file_younger_than_the_threshold_is_not_looked_up(journey):
    payload = sw._pdf(200)
    batch_id = _prepare(journey, payload, stored=payload, minutes_ago=2)  # the default threshold is 3

    assert _pass(journey) == 0
    assert journey.storage.heads == []
    assert _batch(journey, batch_id)["batch_status"] == "AWAITING_UPLOAD"


def test_the_threshold_setting():
    if "AUDIT_CORE_UPLOAD_STORED_CHECK_MINUTES" not in os.environ:
        assert worker._STORED_CHECK_MINUTES == 3.0
    assert worker._stored_check_minutes("5") == 5.0
    assert worker._stored_check_minutes("2.5") == 2.5
    assert worker._stored_check_minutes("0") == 1.0  # never sooner than a minute
    for typo in ("soon", "", "inf", "nan"):
        assert worker._stored_check_minutes(typo) == 3.0, typo  # falls back to the default


def test_slow_storage_leaves_the_rest_of_the_files_for_the_next_pass(journey, monkeypatch):
    payload = sw._pdf(200)
    older = _prepare(journey, payload, stored=payload, minutes_ago=6, upload_id="stored-older")
    newer = _prepare(journey, payload, stored=payload, minutes_ago=5, upload_id="stored-newer")
    monkeypatch.setattr(worker, "_STORED_CHECK_BUDGET_SECONDS", 0.0)  # the time is spent after one file

    assert _pass(journey) == 1
    assert [_batch(journey, b)["batch_status"] for b in (older, newer)] == ["UPLOADED", "AWAITING_UPLOAD"]
    assert _pass(journey) == 1  # the file left over was never looked up: no minute to wait
    assert _batch(journey, newer)["batch_status"] == "UPLOADED"


# ------------------------------------------------------------------- once only

def test_a_second_pass_queues_no_second_split(journey):
    payload = sw._pdf(200)
    batch_id = _prepare(journey, payload, stored=payload, minutes_ago=4)

    assert _pass(journey) == 1
    worker._stored_checked_at.clear()  # as a second worker, which has its own memory
    assert _pass(journey) == 0

    [work] = _split_work(journey, batch_id)
    assert (work["work_status"], work["requested_version"]) == ("PENDING", 1)
    assert len(_accepted(journey, batch_id)) == 1
    assert len(journey.storage.heads) == 1


def test_a_waiting_file_is_looked_up_at_most_once_a_minute(journey):
    payload = sw._pdf(200)
    batch_id = _prepare(journey, payload, stored=None, minutes_ago=4)
    assert _pass(journey) == 0 and len(journey.storage.heads) == 1
    assert _pass(journey) == 0 and len(journey.storage.heads) == 1  # looked up just now

    journey.storage.objects[journey.storage.heads[0]] = payload  # the PUT finishes
    worker._stored_checked_at[(journey.tenant_id, batch_id)] -= 60  # a minute later
    assert _pass(journey) == 1 and len(journey.storage.heads) == 2
    assert _batch(journey, batch_id)["batch_status"] == "UPLOADED"


@pytest.mark.parametrize("meanwhile", ["finalize", "another worker"])
def test_whoever_accepts_first_wins_and_the_file_is_accepted_once(journey, meanwhile):
    payload = sw._pdf(200)
    batch_id = _prepare(journey, payload, stored=payload, minutes_ago=4)

    def accept_meanwhile():  # while this worker's HEAD is out
        if meanwhile == "finalize":
            assert _finalize(journey, batch_id).json() == {"batchId": batch_id, "status": "UPLOADED"}
            return
        own_memory = worker._stored_checked_at
        worker._stored_checked_at = {}
        try:
            assert _pass(journey) == 1
        finally:
            worker._stored_checked_at = own_memory

    journey.storage.on_head = accept_meanwhile
    assert _pass(journey) == 0  # it lost the AWAITING_UPLOAD transition

    assert _batch(journey, batch_id)["batch_status"] == "UPLOADED"
    [work] = _split_work(journey, batch_id)
    assert work["requested_version"] == 1
    [details] = _accepted(journey, batch_id)
    assert ("by" in details) is (meanwhile == "another worker")


# ------------------------------------------------------------------- the 30-minute ceiling is unchanged

def test_a_file_found_whole_only_at_the_last_look_is_not_called_unfinished(journey, monkeypatch):
    """The last look before the ceiling finds it whole: it is accepted, and the
    ceiling (unchanged) then fails it as not split within 30 minutes -- never
    as "did not finish uploading", which would not be true."""
    payload = sw._pdf(200)
    batch_id = _prepare(journey, payload, stored=payload, minutes_ago=31)

    _worker_pass(journey, monkeypatch)

    batch = _batch(journey, batch_id)
    assert (batch["batch_status"], batch["failure_reason"]) == ("FAILED", FILE_NOT_SPLIT_IN_TIME_REASON)
    assert [w["work_status"] for w in _split_work(journey, batch_id)] == ["CANCELLED"]
    assert len(_accepted(journey, batch_id)) == 1


def test_storage_faults_never_stop_the_other_files_or_the_ceiling(journey, monkeypatch):
    payload = sw._pdf(200)
    odd = _prepare(journey, payload, stored=payload, minutes_ago=6, upload_id="stored-odd")
    fine = _prepare(journey, payload, stored=payload, minutes_ago=5, upload_id="stored-fine")
    [odd_key] = [r["original_object_key"] for r in sw._sql(
        journey, "SELECT original_object_key FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND batch_id=:b",
        b=odd)]
    head = journey.storage.head_object

    def flaky(key):
        if key == odd_key:
            raise ValueError("an answer nobody expected")
        return head(key)

    monkeypatch.setattr(journey.storage, "head_object", flaky)
    assert _pass(journey) == 1
    assert _batch(journey, odd)["batch_status"] == "AWAITING_UPLOAD"
    assert _batch(journey, fine)["batch_status"] == "UPLOADED"

    # Storage not configured at all: the ceiling still runs.
    def unconfigured():
        raise RuntimeError("Phase 2 document storage is not configured")

    monkeypatch.setattr(worker, "get_p2_document_storage", unconfigured)
    _age(journey, odd, 31)
    _worker_pass(journey, monkeypatch)
    batch = _batch(journey, odd)
    assert (batch["batch_status"], batch["failure_reason"]) == ("FAILED", FILE_NOT_UPLOADED_IN_TIME_REASON)
    assert _batch(journey, fine)["batch_status"] == "UPLOADED"  # prepared 5 minutes ago: within its 30
