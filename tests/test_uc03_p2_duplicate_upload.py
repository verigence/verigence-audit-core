"""The same file uploaded twice is refused: the second batch is cancelled
before any page is queued, and the Journey says which upload it repeats."""
from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    MemoryStorage,
    create_p2_journey,
    database_engine,
    one_page_pdf,
)
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2dup")
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _batch(journey, storage, filename: str, payload: bytes):
    batch_id = uuid4()
    key = f"p2/{batch_id}/original"
    storage.objects[key] = payload
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_upload_batches (
                    tenant_id, batch_id, journey_id, original_filename, content_type, size_bytes, page_count,
                    original_object_key, batch_status, uploaded_by_actor_id
                ) VALUES (:t, :b, :j, :f, 'application/pdf', :n, 0, :k, 'UPLOADED', :a)
                """
            ),
            {"t": journey.tenant_id, "b": batch_id, "j": journey.journey_id, "f": filename, "n": len(payload),
             "k": key, "a": journey.actor_id},
        )
    return batch_id


def _split(journey, batch_id):
    work = worker.WorkItem(journey.tenant_id, uuid4(), journey.journey_id, "BATCH_SPLIT", str(batch_id), {}, 0, None,
                           None, uuid4())
    worker._split_batch(journey.engine, work)


def _batch_row(journey, batch_id):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return dict(connection.execute(
            text("SELECT batch_status, page_count, sha256, (SELECT COUNT(*) FROM auditcore.p2_document_queue q "
                 "WHERE q.tenant_id=b.tenant_id AND q.batch_id=b.batch_id) AS pages "
                 "FROM auditcore.p2_upload_batches b WHERE tenant_id=:t AND batch_id=:b"),
            {"t": journey.tenant_id, "b": batch_id},
        ).mappings().one())


def test_the_same_file_uploaded_again_is_refused(journey, monkeypatch):
    storage = MemoryStorage()
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(worker, "_owned", lambda *args, **kwargs: None)
    payload = one_page_pdf()
    first = _batch(journey, storage, "booking-pack.pdf", payload)
    _split(journey, first)
    assert _batch_row(journey, first)["batch_status"] == "PROCESSING"
    assert _batch_row(journey, first)["pages"] == 1

    again = _batch(journey, storage, "booking-pack (1).pdf", payload)
    _split(journey, again)
    row = _batch_row(journey, again)
    assert row["batch_status"] == "CANCELLED" and row["pages"] == 0 and row["sha256"] == _batch_row(journey, first)["sha256"]

    # A different file is not a duplicate.
    other = _batch(journey, storage, "receipt.pdf", one_page_pdf(width=300))
    _split(journey, other)
    assert _batch_row(journey, other)["batch_status"] == "PROCESSING"

    # The document list says which upload the refused one repeats; the
    # history records it.
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"
    batches = {b["batchId"]: b for b in client.get(f"{base}/documents").json()["batches"]}
    assert batches[str(again)]["duplicateOf"]["batchId"] == str(first)
    assert batches[str(again)]["duplicateOf"]["filename"] == "booking-pack.pdf"
    assert batches[str(first)]["duplicateOf"] is None and batches[str(other)]["duplicateOf"] is None
    events = client.get(f"{base}/360/audit").json()["events"]
    refused = [e for e in events if e["type"] == "UPLOAD_DUPLICATE"]
    assert len(refused) == 1 and refused[0]["kind"] == "document"
    assert refused[0]["details"]["duplicateOf"] == str(first)


def _scanned_pdf(*images: bytes) -> bytes:
    """A PDF whose pages are each one embedded scan image (raw 2x2 RGB
    pixels, Flate-compressed), as a phone app combines photos into a PDF."""
    import zlib

    objects: list[bytes] = []  # object bodies, 1-based ids

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled once the pages object exists
    pages_id = add(b"")
    page_ids = []
    for pixels in images:
        data = zlib.compress(pixels)
        image_id = add(b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB "
                       b"/BitsPerComponent 8 /Filter /FlateDecode /Length " + str(len(data)).encode()
                       + b" >>\nstream\n" + data + b"\nendstream")
        content = b"q 200 0 0 200 0 0 cm /Im0 Do Q"
        content_id = add(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
        page_ids.append(add(b"<< /Type /Page /Parent " + str(pages_id).encode() + b" 0 R /MediaBox [0 0 200 200] "
                            b"/Resources << /XObject << /Im0 " + str(image_id).encode() + b" 0 R >> >> "
                            b"/Contents " + str(content_id).encode() + b" 0 R >>"))
    objects[catalog - 1] = b"<< /Type /Catalog /Pages " + str(pages_id).encode() + b" 0 R >>"
    objects[pages_id - 1] = (b"<< /Type /Pages /Kids [" + b" ".join(f"{i} 0 R".encode() for i in page_ids)
                             + b"] /Count " + str(len(page_ids)).encode() + b" >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


_SCAN_A = bytes([10, 20, 30] * 4)
_SCAN_B = bytes([200, 210, 220] * 4)
_SCAN_C = bytes([90, 90, 90] * 4)


def _pages(journey, batch_id):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [dict(r) for r in connection.execute(
            text("SELECT page_number, queue_status, status_reason, last_error FROM auditcore.p2_document_queue "
                 "WHERE tenant_id=:t AND batch_id=:b ORDER BY page_number"),
            {"t": journey.tenant_id, "b": batch_id},
        ).mappings().all()]


def _ingest_work(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT COUNT(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND journey_id=:j "
                 "AND work_type='DOCUMENT_INGEST'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()


def test_a_page_already_on_the_journey_is_not_sent_again(journey, monkeypatch):
    storage = MemoryStorage()
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(worker, "_owned", lambda *args, **kwargs: None)
    first = _batch(journey, storage, "monday.pdf", _scanned_pdf(_SCAN_A, _SCAN_B))
    _split(journey, first)
    assert [p["queue_status"] for p in _pages(journey, first)] == ["QUEUED", "QUEUED"]
    assert _ingest_work(journey) == 2

    # A different file that repeats one of Monday's scans: that page is
    # recorded and shown, but not processed again.
    second = _batch(journey, storage, "tuesday.pdf", _scanned_pdf(_SCAN_B, _SCAN_C))
    _split(journey, second)
    pages = _pages(journey, second)
    assert pages[0]["queue_status"] == "CANCELLED" and pages[0]["last_error"] == "DUPLICATE_PAGE"
    assert pages[0]["status_reason"] == "Same as page 2 of monday.pdf; already uploaded, not sent again."
    assert pages[1]["queue_status"] == "QUEUED"
    assert _ingest_work(journey) == 3
    assert _batch_row(journey, second)["batch_status"] == "PROCESSING"

    # The same scan twice in one file, both already on the Journey: every
    # page points at the copy that is being processed; nothing is sent.
    third = _batch(journey, storage, "wednesday.pdf", _scanned_pdf(_SCAN_C, _SCAN_C))
    _split(journey, third)
    pages = _pages(journey, third)
    assert [p["status_reason"] for p in pages] == ["Same as page 2 of tuesday.pdf; already uploaded, not sent again."] * 2
    assert _batch_row(journey, third)["batch_status"] == "CANCELLED"
    assert _ingest_work(journey) == 3

    # The same new scan twice in one file: the second copy is the duplicate of the first.
    fourth = _batch(journey, storage, "thursday.pdf", _scanned_pdf(_SCAN_A[::-1], _SCAN_A[::-1]))
    _split(journey, fourth)
    pages = _pages(journey, fourth)
    assert pages[0]["queue_status"] == "QUEUED"
    assert pages[1]["status_reason"] == "Same as page 1 of thursday.pdf; already uploaded, not sent again."
    assert _ingest_work(journey) == 4

    # The list shows every page, the duplicates as already uploaded.
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"
    batches = {b["batchId"]: b for b in client.get(f"{base}/documents").json()["batches"]}
    units = batches[str(second)]["documents"]
    assert [u["queue_status"] for u in units] == ["CANCELLED", "QUEUED"]
    assert units[0]["status_reason"].startswith("Same as page 2 of monday.pdf")


def test_a_page_rejected_for_scan_quality_is_final(journey, monkeypatch):
    storage = MemoryStorage()
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(worker, "_owned", lambda *args, **kwargs: None)
    batch = _batch(journey, storage, "blurred.pdf", _scanned_pdf(_SCAN_A))
    _split(journey, batch)
    rejected = worker.classify_page_outcome(
        di_item={"state": "FAILED", "failureCode": "DI_QUALITY_IMAGE_BLUR_SCORE",
                 "failureDetail": "Blur score 42.0 below threshold 100.0"},
        extracted_count=0, submitted_at=None, processed_seen_at=None, now=worker.datetime.now(worker.UTC),
    )
    assert rejected.status == "FAILED"
    assert rejected.reason == ("Page rejected: Blur score 42.0 below threshold 100.0. "
                               "Re-scan this page and upload it again; retrying will not help.")
    unreadable = worker.classify_page_outcome(
        di_item={"state": "FAILED", "failureCode": "INVALID_FILE_CONTENT", "failureDetail": "not a PDF"},
        extracted_count=0, submitted_at=None, processed_seen_at=None, now=worker.datetime.now(worker.UTC),
    )
    assert unreadable.reason == "The file could not be read: not a PDF. Upload it again."

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        queue_id = connection.execute(
            text("UPDATE auditcore.p2_document_queue SET queue_status='FAILED', status_reason=:r, "
                 "last_error='DI_QUALITY_IMAGE_BLUR_SCORE' WHERE tenant_id=:t AND batch_id=:b RETURNING queue_id"),
            {"t": journey.tenant_id, "b": batch, "r": rejected.reason},
        ).scalar_one()
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"
    response = client.post(f"{base}/pages/{queue_id}:retry")
    assert response.status_code == 409
    assert "Re-scan it" in response.json()["detail"]
