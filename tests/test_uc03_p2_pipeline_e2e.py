"""Phase 2 pipeline end to end on Postgres: API upload -> finalize -> split ->
DI ingest -> reconcile -> stage -> controls -> tasks -> Journey 360, driven by
the real worker loop, with a fake DI that dedupes by clientUploadId (as DI
does) and a crash injected between DI's upload intent and finalize.

What must hold: every upload becomes exactly one DI document (no duplicates
on retry), every document is read exactly once (a page of an always-merged
type is only classified; its document is uploaded once, trusted, and read),
everything settles, the stage and the 360 read model see the facts, and no
work item is dead-lettered."""
from __future__ import annotations

import io
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_evidence,
    add_extracted_field,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
    set_minimum_booking_amount,
)
from pypdf import PdfWriter
from sqlalchemy import text

from audit_core import uc03_p2_api as api
from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client

PAGE_TYPES = {1: "booking_form", 2: "dealer_receipt", 3: "pan_card"}
# DI's requirement refs for the journey: DI reads a document only against one.
REQUIREMENT_REFS = {"booking_form": "ref-booking", "dealer_receipt": "ref-receipt", "pan_card": "ref-pan"}
PAGE_FACTS = {
    "booking_form": {"customer_name": "SAMPLE CUSTOMER", "booking_amount_paid": "21000", "total_price": "1245000"},
    "dealer_receipt": {"amount_paid": "21000", "receipt_number": "RCPT-1"},
    "pan_card": {"pan_number": "ABCDE1234F", "pan_name": "SAMPLE CUSTOMER"},
}


class Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def presign_put(self, key, *, content_type, expires_seconds=900):
        return f"https://storage.test/{key}"

    def head_object(self, key):
        return {"contentLength": len(self.objects[key]), "contentType": "application/pdf", "etag": "e"}

    def put_object(self, key, data, *, content_type):
        self.objects[key] = data

    def get_object(self, key):
        return self.objects[key]

    def presign_get(self, key, *, expires_seconds=1800):
        return f"https://storage.test/get/{key}"


class FakeDI:
    """DI Capture V2 double. Deduplicates by clientUploadId like DI does,
    classifies from the page (or trusts a single candidate), reads a
    document only when its type has a requirement ref, and reports it
    PROCESSED on its second listing, delivering its facts to Audit Core
    (standing in for DI's fact sync). Counts provider work."""

    def __init__(self, journey) -> None:
        self.journey = journey
        self.by_client: dict[str, dict] = {}
        self.intent_calls = 0
        self.fail_next_finalize = True
        self.listings: dict[str, int] = {}
        self.classifications = 0
        self.extractions: list[str] = []

    def create_upload_intents(self, *, token, tenant_id, external_context_ref, phase, files,
                              candidate_document_type_keys, requirement_refs_by_document_type_key,
                              classification_mode=None):
        self.intent_calls += 1
        trusted = classification_mode == "TRUST_SINGLE_CANDIDATE"
        assert not trusted or len(candidate_document_type_keys) == 1
        uploads = []
        for f in files:
            doc = self.by_client.get(f["clientUploadId"])
            if doc is None:
                name = f["filename"]
                page = int((name.rsplit("-pages-", 1) if "-pages-" in name else name.rsplit("-page-", 1))[1][:3])
                doc_type = candidate_document_type_keys[0] if trusted else PAGE_TYPES[page]
                doc = {"documentId": str(uuid4()), "clientUploadId": f["clientUploadId"], "page": page,
                       "type": doc_type, "finalized": False, "trusted": trusted,
                       "read": doc_type in (requirement_refs_by_document_type_key or {})}
                self.by_client[f["clientUploadId"]] = doc
            uploads.append({"documentId": doc["documentId"], "uploadUrl": f"https://di.test/{doc['documentId']}",
                            "uploadHeaders": {}})
        return {"uploads": uploads}

    def finalize_document(self, *, token, tenant_id, external_context_ref, document_id):
        if self.fail_next_finalize:
            self.fail_next_finalize = False
            raise RuntimeError("simulated worker crash between DI intent and finalize")
        doc = next(d for d in self.by_client.values() if d["documentId"] == document_id)
        if not doc["finalized"] and not doc["trusted"]:
            self.classifications += 1
        doc["finalized"] = True

    def list_documents(self, *, token, tenant_id, external_context_ref, phase):
        if phase != "BOOKING":
            return {"documents": []}
        items = []
        for doc in self.by_client.values():
            if not doc["finalized"]:
                continue
            seen = self.listings.get(doc["documentId"], 0) + 1
            self.listings[doc["documentId"]] = seen
            processed = seen >= 2 and doc["read"]
            if processed and not doc.get("facts"):
                self._deliver_facts(doc)
            items.append({
                "documentId": doc["documentId"], "state": "CLASSIFIED",
                "processingStatus": "PROCESSED" if processed else ("PROCESSING" if doc["read"] else "NOT_STARTED"),
                "classifiedDocumentTypeKey": doc["type"],
            })
        return {"documents": items}

    def delete_document(self, **_):
        pass

    def _deliver_facts(self, doc) -> None:
        from uuid import UUID

        document_id = UUID(doc["documentId"])
        add_evidence(self.journey, di_document_id=document_id, document_type_key=doc["type"])
        for key, value in PAGE_FACTS[doc["type"]].items():
            add_extracted_field(self.journey, di_document_id=document_id, field_key=key, value=value,
                                document_type=doc["type"], confidence=99.0)
        if doc["type"] == "dealer_receipt":
            add_receipt_payment(self.journey, amount="21000", receipt_number="RCPT-1",
                                receipt_date="2026-09-08", di_document_id=document_id)
        doc["facts"] = True
        self.extractions.append(doc["type"])


class _Put:
    def __init__(self, *_, **__):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def put(self, url, headers=None, content=None):
        return SimpleNamespace(raise_for_status=lambda: None)


def _three_page_pdf() -> bytes:
    writer = PdfWriter()
    for width in (200, 300, 400):
        writer.add_blank_page(width=width, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture
def pipeline(monkeypatch):
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="p2e2e")
    set_minimum_booking_amount(journey, "21000")
    storage = Storage()
    di = FakeDI(journey)
    monkeypatch.setattr(api, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(worker, "get_di_capture_v2_client", lambda: di)
    monkeypatch.setattr(worker, "httpx", SimpleNamespace(Client=_Put))
    monkeypatch.setattr(worker, "_di_context_and_requirements",
                        lambda engine, work: ("ctx", "tok", list(PAGE_TYPES.values()), dict(REQUIREMENT_REFS)))
    # only this test's tenant is worked
    monkeypatch.setattr(worker, "_active_tenants", lambda engine: [journey.tenant_id])
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        yield journey, storage, di
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()


def _drain(journey, rounds: int = 60) -> None:
    """Run the real worker loop; delayed work is made due immediately."""
    for _ in range(rounds):
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            connection.execute(
                text("UPDATE auditcore.p2_work_queue SET next_attempt_at_utc=now() "
                     "WHERE tenant_id=:t AND work_status IN ('PENDING','RETRY_WAIT')"),
                {"t": journey.tenant_id},
            )
        if worker.run_once(journey.engine) == 0:
            return
    raise AssertionError("the P2 pipeline did not settle")


def test_upload_to_journey_360_end_to_end(pipeline):
    journey, storage, di = pipeline
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"
    pdf = _three_page_pdf()

    init = client.post(f"{base}/uploads:init", json={"files": [{
        "filename": "booking-packet.pdf", "contentType": "application/pdf", "sizeBytes": len(pdf),
        "clientUploadId": "e2e-packet-0001"}]})
    assert init.status_code == 200, init.text
    [upload] = init.json()["uploads"]
    storage.objects[upload["uploadUrl"].split("https://storage.test/", 1)[1]] = pdf
    assert client.post(f"{base}/uploads/{upload['batchId']}:finalize").status_code == 202
    # A retried finalize is idempotent.
    assert client.post(f"{base}/uploads/{upload['batchId']}:finalize").status_code in {200, 202}

    _drain(journey)

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        pages = connection.execute(
            text("SELECT page_number, queue_status, di_document_id FROM auditcore.p2_document_queue "
                 "WHERE tenant_id=:t AND unit_kind='PAGE' ORDER BY page_number"), {"t": journey.tenant_id},
        ).mappings().all()
        groups = connection.execute(
            text("SELECT page_numbers, queue_status, di_document_id, template_key FROM auditcore.p2_document_queue "
                 "WHERE tenant_id=:t AND unit_kind='GROUP'"), {"t": journey.tenant_id},
        ).mappings().all()
        dead = connection.execute(
            text("SELECT work_type, last_error FROM auditcore.p2_work_queue "
                 "WHERE tenant_id=:t AND work_status='DEAD_LETTER'"), {"t": journey.tenant_id},
        ).all()
        stage = connection.execute(
            text("SELECT current_stage FROM auditcore.p2_journey_runtime WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()
        controls = connection.execute(
            text("SELECT COUNT(*) FROM auditcore.p2_control_state WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()

    assert [p["page_number"] for p in pages] == [1, 2, 3]
    # The booking form page (an always-merged type) was only classified and
    # became one trusted document; the receipt and PAN pages were read as is.
    assert [p["queue_status"] for p in pages] == ["MERGED", "READY", "READY"], pages
    [group] = groups
    assert list(group["page_numbers"]) == [1] and group["queue_status"] == "READY"
    assert group["template_key"] == "booking_docket"
    assert not dead, dead
    # The injected crash was retried with the same clientUploadId: one DI
    # document per upload, never a duplicate.
    assert len(di.by_client) == 4 and di.intent_calls == 5
    assert {str(u["di_document_id"]) for u in [*pages, *groups]} == {d["documentId"] for d in di.by_client.values()}
    # Gemini work: three classifications (one per page, none for the trusted
    # document) and every business document read exactly once.
    assert di.classifications == 3
    assert sorted(di.extractions) == sorted(PAGE_TYPES.values())
    assert stage.startswith("BOOKING")
    assert controls > 0

    summary = client.get(f"{base}/360").json()
    assert summary["numbers"]["documents"] == 3
    assert summary["stage"]["gates"]["MINIMUM_BOOKING_PAYMENT"]["passed"] is True
    documents = client.get(f"{base}/360/documents").json()["documents"]
    assert {d["documentType"] for d in documents} == set(PAGE_TYPES.values())
    assert client.get(f"{base}/360/compliance-report").status_code == 200


def test_worker_that_dies_mid_batch_loses_nothing(pipeline):
    """A worker claims work and dies without finishing: once its leases
    expire, another worker picks everything up and the batch completes with
    no duplicate DI documents."""
    journey, storage, di = pipeline
    di.fail_next_finalize = False
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"
    pdf = _three_page_pdf()
    [upload] = client.post(f"{base}/uploads:init", json={"files": [{
        "filename": "booking-packet.pdf", "contentType": "application/pdf", "sizeBytes": len(pdf),
        "clientUploadId": "e2e-packet-0002"}]}).json()["uploads"]
    storage.objects[upload["uploadUrl"].split("https://storage.test/", 1)[1]] = pdf
    assert client.post(f"{base}/uploads/{upload['batchId']}:finalize").status_code == 202

    # Split, then a worker claims the ingest items and "crashes".
    worker.run_once(journey.engine)
    crashed = worker._claim_for_tenant(journey.engine, journey.tenant_id, 20)
    assert crashed, "expected claimed work to abandon"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_work_queue SET lease_expires_at_utc=now() - interval '1 second' "
                 "WHERE tenant_id=:t AND work_status IN ('CLAIMED','PROCESSING')"), {"t": journey.tenant_id},
        )

    _drain(journey)
    # The crashed worker's late completion is rejected, never applied twice.
    for item in crashed:
        worker._settle(journey.engine, item, worker._complete)

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        statuses = connection.execute(
            text("SELECT queue_status FROM auditcore.p2_document_queue WHERE tenant_id=:t AND unit_kind='PAGE'"),
            {"t": journey.tenant_id},
        ).scalars().all()
        open_work = connection.execute(
            text("SELECT COUNT(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t "
                 "AND work_status NOT IN ('COMPLETED','CANCELLED')"), {"t": journey.tenant_id},
        ).scalar_one()
    assert sorted(statuses) == ["MERGED", "READY", "READY"]
    assert len(di.by_client) == 4
    assert sorted(di.extractions) == sorted(PAGE_TYPES.values())
    assert open_work == 0


def test_a_two_page_booking_form_is_read_once_and_never_classified_twice(pipeline, monkeypatch):
    """Booking form pages 1 and 3 plus a receipt: each page is classified
    once, the booking form pages are not read on their own, and the merged
    booking form is uploaded trusted (no second classification) and read
    once. Before: 4 classifications and 4 extractions; now 3 and 2."""
    journey, storage, di = pipeline
    di.fail_next_finalize = False
    monkeypatch.setitem(PAGE_TYPES, 3, "booking_form")
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"
    pdf = _three_page_pdf()
    [upload] = client.post(f"{base}/uploads:init", json={"files": [{
        "filename": "booking-packet.pdf", "contentType": "application/pdf", "sizeBytes": len(pdf),
        "clientUploadId": "e2e-packet-0003"}]}).json()["uploads"]
    storage.objects[upload["uploadUrl"].split("https://storage.test/", 1)[1]] = pdf
    assert client.post(f"{base}/uploads/{upload['batchId']}:finalize").status_code == 202

    _drain(journey)

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        units = connection.execute(
            text("SELECT unit_kind, page_numbers, queue_status FROM auditcore.p2_document_queue "
                 "WHERE tenant_id=:t ORDER BY unit_kind, page_number"), {"t": journey.tenant_id},
        ).all()
    assert [(u[0], list(u[1]), u[2]) for u in units] == [
        ("GROUP", [1, 3], "READY"),
        ("PAGE", [1], "MERGED"), ("PAGE", [2], "READY"), ("PAGE", [3], "MERGED"),
    ]
    assert di.classifications == 3
    assert sorted(di.extractions) == ["booking_form", "dealer_receipt"]
    trusted = [d for d in di.by_client.values() if d["trusted"]]
    assert len(trusted) == 1 and trusted[0]["type"] == "booking_form"
