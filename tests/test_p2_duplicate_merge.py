"""DUPLICATE_MERGE (2026-11-02): the documents only a duplicate copy holds go to the booking kept.

Suryakant's pair on Mahindra Orissa: the kept booking lacked the PAN the copy
held, so the copy could not be deleted without losing it. The job copies the
copy's stored PAN page to a new upload of the kept booking, split and read
there like any upload; the copy is untouched (it is deleted afterwards by the
purge job)."""
from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
import test_p2_dayend_sweep as sw
from conftest import delete_tenant_data
from p2_support import MemoryStorage, create_p2_journey, database_engine, one_page_pdf
from sqlalchemy import text

from audit_core import uc03_duplicate_merge as job
from audit_core import uc03_p2_storage
from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context


@pytest.fixture
def pair(monkeypatch):
    storage = MemoryStorage()
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(uc03_p2_storage, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(worker, "_owned", lambda *args, **kwargs: None)
    engine = database_engine()
    keep = create_p2_journey(engine, prefix="p2merge")
    keep.storage = storage  # type: ignore[attr-defined]
    with engine.begin() as connection:
        set_tenant_context(connection, keep.tenant_id)
        customer_id = connection.execute(
            text("INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, customer_type_code, display_name) "
                 "VALUES (:t, :d, :o, 'INDIVIDUAL', 'SURYAKANT') RETURNING customer_id"),
            {"t": keep.tenant_id, "d": keep.dealer_id, "o": keep.outlet_id}).scalar_one()
        copy_id = connection.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference, "
                 "created_by_actor_id) VALUES (:t, :d, :o, :c, :r, :a) RETURNING journey_id"),
            {"t": keep.tenant_id, "d": keep.dealer_id, "o": keep.outlet_id, "c": customer_id,
             "r": f"COPY-{uuid4().hex[:6]}", "a": keep.actor_id}).scalar_one()
        connection.execute(text("UPDATE auditcore.journeys SET created_by_actor_id=:a WHERE tenant_id=:t AND "
                                "journey_id=:j"), {"t": keep.tenant_id, "a": keep.actor_id, "j": keep.journey_id})
    copy = replace(keep, journey_id=copy_id, customer_id=customer_id)
    try:
        yield keep, copy, storage
    finally:
        delete_tenant_data(engine, keep.tenant_id)
        engine.dispose()


def _ref(journey) -> str:
    return sw._sql(journey, "SELECT journey_reference FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j")[0][
        "journey_reference"]


def _read_document(journey, storage, kind: str) -> str:
    """One page read on ``journey`` as ``kind``, its stored page file kept."""
    batch_id, queue_id, document_id = uuid4(), uuid4(), uuid4()
    key = f"p2/{batch_id}/pages/0001.pdf"
    storage.objects[key] = one_page_pdf(321)
    sw._sql(journey, "INSERT INTO auditcore.p2_upload_batches (tenant_id, batch_id, journey_id, original_filename, "
                     "content_type, size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id) "
                     "VALUES (:t, :b, :j, 'suryakant booking.pdf', 'application/pdf', 10, 1, :k, 'COMPLETED', :a)",
            b=batch_id, k=f"p2/{batch_id}/original", a=journey.actor_id)
    sw._sql(journey, "INSERT INTO auditcore.p2_document_queue (tenant_id, queue_id, batch_id, journey_id, page_number, "
                     "page_numbers, page_sha256, page_object_key, client_upload_id, di_document_id, queue_status, "
                     "classified_document_type, business_stage) VALUES (:t, :q, :b, :j, 1, ARRAY[1], :sha, :k, :c, "
                     ":d, 'READY', :kind, 'BOOKING')",
            q=queue_id, b=batch_id, sha=uuid4().hex, k=key, c=f"p2-{uuid4().hex}", d=document_id, kind=kind)
    sw._sql(journey, "INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id, di_document_id, "
                     "document_type_key, evidence_purpose, process_area, association_status, linked_at_utc) "
                     "VALUES (:t, :j, :c, :s, :d, :kind, 'BOOKING_DOCUMENT', 'BOOKING', 'ACTIVE', now())",
            c=journey.customer_id, s=uuid4(), d=document_id, kind=kind)
    return key


def _run(keep, label: str, entries: list[str]) -> dict:
    return job.run_duplicate_merge(keep.engine, f"{keep.tenant_id}:{label}", entries)


def test_the_document_only_the_copy_holds_is_copied_to_the_kept_booking_and_read_there(pair):
    keep, copy, storage = pair
    page_key = _read_document(copy, storage, "pan_card")
    _read_document(copy, storage, "aadhaar")
    _read_document(keep, storage, "aadhaar")
    entry = f"{_ref(keep)}>{_ref(copy)}:pan_card,aadhaar"
    dry = _run(keep, "merge", [entry])
    assert [(i["documentType"], i["outcome"]) for i in dry["items"]] == [
        ("pan_card", "WOULD_COPY"), ("aadhaar", "SKIPPED")]
    assert dry["items"][1]["why"] == "the kept booking already holds one"
    assert len(sw._sql(keep, "SELECT 1 FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND journey_id=:j")) == 1

    done = _run(keep, "merge-commit", [entry])
    [copied] = [i for i in done["items"] if i["outcome"] == "COPIED"]
    [batch] = sw._sql(keep, "SELECT batch_status, original_object_key, original_filename, uploaded_by_actor_id "
                            "FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND batch_id=:b", b=copied["newBatchId"])
    assert batch["batch_status"] == "UPLOADED" and batch["uploaded_by_actor_id"] == keep.actor_id
    assert batch["original_filename"].startswith(f"pan_card from {_ref(copy)} - suryakant booking page 1")
    assert f"/{keep.journey_id}/" in batch["original_object_key"]
    assert storage.objects[batch["original_object_key"]] == storage.objects[page_key]
    # Split and read on the kept booking like any upload.
    sw._split(keep, copied["newBatchId"])
    assert sw._sql(keep, "SELECT queue_status FROM auditcore.p2_document_queue WHERE tenant_id=:t AND batch_id=:b",
                   b=copied["newBatchId"]) == [{"queue_status": "QUEUED"}]
    # Both histories say so; the copy is untouched.
    assert sw._sql(keep, "SELECT 1 FROM auditcore.p2_activity_events WHERE tenant_id=:t AND journey_id=:j "
                         "AND event_type='DOCUMENT_COPIED_FROM_DUPLICATE'")
    assert sw._sql(copy, "SELECT 1 FROM auditcore.p2_activity_events WHERE tenant_id=:t AND journey_id=:j "
                         "AND event_type='DOCUMENT_COPIED_TO_KEPT_BOOKING'")
    assert len(sw._sql(copy, "SELECT 1 FROM auditcore.evidence WHERE tenant_id=:t AND journey_id=:j "
                             "AND association_status='ACTIVE'")) == 2
    # A second run copies nothing again.
    again = _run(keep, "merge-commit", [entry])
    assert [i["outcome"] for i in again["items"] if i["documentType"] == "pan_card"] == ["COPIED"]
    assert len(sw._sql(keep, "SELECT 1 FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND journey_id=:j")) == 2


def test_a_closed_booking_a_bad_entry_or_a_type_the_copy_lacks_copies_nothing(pair):
    keep, copy, storage = pair
    _read_document(copy, storage, "pan_card")
    assert _run(keep, "merge-commit", ["nonsense"])["items"][0]["outcome"] == "FAILED"
    [item] = _run(keep, "merge-commit", [f"{_ref(keep)}>{_ref(copy)}:customer_kyc"])["items"]
    assert (item["outcome"], item["why"]) == ("SKIPPED", "the copy holds none read from a stored file")
    sw._sql(copy, "INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, business_status) "
                  "VALUES (:t, :j, 'BOOKING', 'BOOKING_CANCELLED')")
    [item] = _run(keep, "merge-commit", [f"{_ref(keep)}>{_ref(copy)}:pan_card"])["items"]
    assert item["outcome"] == "SKIPPED" and "cancelled" in item["why"]
    assert len(sw._sql(keep, "SELECT 1 FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND journey_id=:j")) == 0
