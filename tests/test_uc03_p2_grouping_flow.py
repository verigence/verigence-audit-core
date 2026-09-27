"""End-to-end page grouping on Postgres with in-memory storage and a fake DI."""
from __future__ import annotations

import io

import pytest
from conftest import delete_tenant_data
from p2_support import (
    MemoryStorage,
    add_batch_pages,
    add_evidence,
    create_p2_journey,
    database_engine,
    one_page_pdf,
    queue_row,
)
from pypdf import PdfReader
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context


class FakeCapture:
    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}
        self.deleted: list[str] = []

    def list_documents(self, *, token, tenant_id, external_context_ref, phase):
        return {"documents": list(self.documents.values()) if phase == "BOOKING" else []}

    def delete_document(self, *, token, tenant_id, external_context_ref, document_id):
        self.deleted.append(document_id)
        self.documents.pop(document_id, None)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2grp")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _run(journey, work_type, key):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker._enqueue(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                        work_type=work_type, work_key=key, payload={}, correlation_id=None)
    items = [i for i in worker._claim_for_tenant(journey.engine, journey.tenant_id, 20)
             if i.work_type == work_type and i.work_key == key]
    item = items[0]
    handler = {"JOURNEY_RECONCILE": worker._journey_reconcile, "BATCH_GROUP": worker._group_batch}[work_type]
    try:
        handler(journey.engine, item)
    except worker.RescheduleWork as exc:
        worker._reschedule(journey.engine, item, exc)
        return "RESCHEDULED"
    worker._complete(journey.engine, item)
    return "DONE"


def test_split_aadhaar_is_regrouped_resubmitted_and_fragments_retired(journey, monkeypatch):
    storage = MemoryStorage()
    capture = FakeCapture()
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(worker, "get_di_capture_v2_client", lambda: capture)
    monkeypatch.setattr(worker, "_di_context_and_requirements", lambda engine, work: ("ctx", "tok", [], {}))

    batch_id, pages = add_batch_pages(
        journey, [("aadhaar", "EXTRACTING"), ("aadhaar", "EXTRACTING"), ("pan_card", "EXTRACTING")],
    )
    for index, page in enumerate(pages):
        storage.objects[page["object_key"]] = one_page_pdf(100 * (index + 1))
        capture.documents[str(page["di_document_id"])] = {
            "documentId": str(page["di_document_id"]), "state": "CLASSIFIED", "processingStatus": "PROCESSING",
        }
    # the front page's facts already reached Audit Core before grouping
    add_evidence(journey, di_document_id=pages[0]["di_document_id"], document_type_key="aadhaar")

    assert _run(journey, "JOURNEY_RECONCILE", str(journey.journey_id)) == "RESCHEDULED"
    assert queue_row(journey, "BATCH_GROUP", str(batch_id)) is not None

    assert _run(journey, "BATCH_GROUP", str(batch_id)) == "DONE"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        units = connection.execute(
            text("SELECT unit_kind, page_numbers, queue_status, merged_into_queue_id, candidate_override, "
                 "page_object_key, template_key FROM auditcore.p2_document_queue "
                 "WHERE tenant_id=:t AND batch_id=:b ORDER BY unit_kind, page_number"),
            {"t": journey.tenant_id, "b": batch_id},
        ).mappings().all()
    group = [u for u in units if u["unit_kind"] == "GROUP"]
    assert len(group) == 1
    assert group[0]["page_numbers"] == [1, 2]
    assert group[0]["candidate_override"] == ["aadhaar"]
    assert group[0]["template_key"] == "aadhaar"
    merged_pdf = PdfReader(io.BytesIO(storage.objects[group[0]["page_object_key"]]))
    assert [float(p.mediabox.width) for p in merged_pdf.pages] == [100.0, 200.0]
    statuses = {tuple(u["page_numbers"]): u["queue_status"] for u in units if u["unit_kind"] == "PAGE"}
    assert statuses == {(1,): "MERGED", (2,): "MERGED", (3,): "EXTRACTING"}
    group_id = str(next(u for u in units if u["unit_kind"] == "PAGE" and u["page_numbers"] == [1])["merged_into_queue_id"])
    assert queue_row(journey, "DOCUMENT_INGEST", group_id) is not None

    # Next reconcile retires the fragments: DI copies deleted, evidence voided.
    _run(journey, "JOURNEY_RECONCILE", str(journey.journey_id))
    assert set(capture.deleted) == {str(pages[0]["di_document_id"]), str(pages[1]["di_document_id"])}
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        status = connection.execute(
            text("SELECT association_status FROM auditcore.evidence WHERE tenant_id=:t AND di_document_id=:d"),
            {"t": journey.tenant_id, "d": pages[0]["di_document_id"]},
        ).scalar_one()
    assert status == "VOIDED"


def test_grouping_waits_until_every_page_is_classified(journey, monkeypatch):
    monkeypatch.setattr(worker, "get_di_capture_v2_client", lambda: FakeCapture())
    monkeypatch.setattr(worker, "_di_context_and_requirements", lambda engine, work: ("ctx", "tok", [], {}))
    batch_id, _ = add_batch_pages(journey, [("aadhaar", "EXTRACTING"), (None, "CLASSIFYING")])
    _run(journey, "JOURNEY_RECONCILE", str(journey.journey_id))
    assert queue_row(journey, "BATCH_GROUP", str(batch_id)) is None
