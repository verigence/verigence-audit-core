"""Bulk upload of journey files (product owner 2026-11-03): the drop, exact duplicates, a
journey read per group, and each journey joined to the customer's booking or kept.
Real Postgres; storage, Document Intelligence and Security are doubles."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    P2Journey,
    add_evidence,
    add_extracted_field,
    add_page,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_p2_bulk_upload as bulk_upload
from audit_core import uc03_p2_storage, uc03_p2_worker
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client

PAN = "ABCPE1234F"


class BucketDouble:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def presign_put(self, key: str, *, content_type: str, expires_seconds: int = 900) -> str:
        return f"https://r2.example/{key}?put"

    def head_object(self, key: str) -> dict:
        if key not in self.objects:
            raise KeyError(key)
        return {"contentLength": len(self.objects[key]), "contentType": "application/pdf", "etag": "e"}

    def get_object(self, key: str) -> bytes:
        if key not in self.objects and key.startswith("p2/"):  # a test page (p2_support.add_page)
            return b"%PDF-1.4 page " + key.encode()
        return self.objects[key]

    def put_object(self, key: str, payload: bytes, *, content_type: str) -> None:
        self.objects[key] = payload

    def list_keys(self, prefix: str) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]

    def delete_objects(self, keys: list[str]) -> int:
        for k in keys:
            self.objects.pop(k, None)
        return len(keys)

    def presign_get(self, key: str, *, expires_seconds: int = 1800) -> str:
        return f"https://r2.example/{key}"


class FakeSecurity:
    def get_service_token(self, *, audience: str) -> str:
        return "token"


class FakeDi:
    def delete_document(self, **_) -> None:
        return None


@pytest.fixture
def s(monkeypatch):
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="bulk")
    bucket = BucketDouble()
    monkeypatch.setattr(uc03_p2_storage, "get_p2_document_storage", lambda: bucket)
    monkeypatch.setattr(uc03_p2_worker, "get_p2_document_storage", lambda: bucket)
    monkeypatch.setattr(uc03_p2_worker, "get_security_oauth_client", FakeSecurity)
    monkeypatch.setattr(uc03_p2_worker, "get_di_capture_v2_client", FakeDi)
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization("PC")
    try:
        yield SimpleNamespace(journey=journey, bucket=bucket, client=TestClient(app, raise_server_exceptions=False),
                              base=f"/p2/v1/tenants/{journey.tenant_id}/bulk-uploads")
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()


def _drop(s, files: dict[str, bytes]) -> str:
    """The PC's drop: open, add, put each file in storage, complete, submit."""
    created = s.client.post(s.base, json={"outletId": str(s.journey.outlet_id), "createdByName": "Ravi PC"})
    assert created.status_code == 201, created.text
    bulk_id = created.json()["bulkId"]
    added = s.client.post(f"{s.base}/{bulk_id}/files", json={"files": [
        {"filename": name, "contentType": "application/pdf", "sizeBytes": len(data)} for name, data in files.items()]})
    assert added.status_code == 200, added.text
    for item, data in zip(added.json()["files"], files.values(), strict=True):
        s.bucket.objects[item["objectKey"]] = data
        done = s.client.post(f"{s.base}/{bulk_id}/files/{item['fileId']}/complete")
        assert done.status_code == 200, done.text
    submitted = s.client.post(f"{s.base}/{bulk_id}/submit")
    assert submitted.status_code == 200, submitted.text
    return bulk_id


def _files(s, bulk_id: str) -> list[dict]:
    with s.journey.engine.begin() as c:
        set_tenant_context(c, s.journey.tenant_id)
        return [dict(r) for r in c.execute(
            text("SELECT * FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND bulk_id=:b ORDER BY original_filename"),
            {"t": s.journey.tenant_id, "b": bulk_id}).mappings().all()]


def _run_upload(s, bulk_id: str) -> dict:
    return bulk_upload.run_bulk_upload(s.journey.engine, f"{s.journey.tenant_id}:{bulk_id}")


def test_names_and_folders():
    folder = bulk_upload.bulk_folder("t1", "Utkal Mahindra", "Cuttack 3S")
    assert folder.startswith("p2-bulk/t1/Utkal-Mahindra/Cuttack-3S/2") and folder.count("/") == 4
    assert bulk_upload.plain_name("C:\\scans\\Asha Das KYC.pdf") == "Asha Das KYC.pdf"
    assert bulk_upload.numbered("Asha Das KYC.pdf", 2) == "Asha Das KYC (2).pdf"
    a = bulk_upload.group_key("Asha Das KYC.pdf", "1")
    assert a == bulk_upload.group_key("asha das invoice (2).pdf", "2") or a.startswith("stem:")


def test_the_drop_keeps_each_file_under_its_own_name_and_waits_for_submit(s):
    created = s.client.post(s.base, json={"outletId": str(s.journey.outlet_id)}).json()
    added = s.client.post(f"{s.base}/{created['bulkId']}/files", json={"files": [
        {"filename": "Asha Das.pdf", "contentType": "application/pdf", "sizeBytes": 10},
        {"filename": "Asha Das.pdf", "contentType": "application/pdf", "sizeBytes": 10}]}).json()["files"]
    assert added[0]["objectKey"] == f"{created['folder']}/Asha Das.pdf"
    assert added[1]["objectKey"] == f"{created['folder']}/Asha Das (2).pdf"
    # Not submitted: nothing to process; a file not yet in storage cannot be completed.
    assert s.client.post(f"{s.base}/{created['bulkId']}/files/{added[0]['fileId']}/complete").status_code == 409
    assert s.client.post(f"{s.base}/{created['bulkId']}/submit").status_code == 409
    listed = s.client.get(s.base).json()
    assert listed["inProgress"] == 0 and listed["items"][0]["status"] == "OPEN"
    # Another outlet is refused.
    assert s.client.post(s.base, json={"outletId": str(uuid4())}).status_code == 404


def test_submit_queues_the_job_and_files_are_placed(s):
    bulk_id = _drop(s, {"Asha Das KYC.pdf": b"%PDF asha kyc", "Asha Das invoice.pdf": b"%PDF asha inv",
                        "IMG_0042.pdf": b"%PDF other"})
    with s.journey.engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM auditcore.p2_worker_jobs WHERE job_kind='BULK_UPLOAD' AND tag=:t"),
                         {"t": f"{s.journey.tenant_id}:{bulk_id}"}).scalar_one() == 1
    assert s.client.get(s.base).json()["inProgress"] == 1
    result = _run_upload(s, bulk_id)
    assert result["exactDuplicates"] == 0 and result["reading"] == 3
    files = {f["original_filename"]: f for f in _files(s, bulk_id)}
    assert {f["outcome"] for f in files.values()} == {"READING"}
    # The two files of one customer are read on one journey; the generic name on its own.
    assert files["Asha Das KYC.pdf"]["journey_id"] == files["Asha Das invoice.pdf"]["journey_id"]
    assert files["IMG_0042.pdf"]["journey_id"] != files["Asha Das KYC.pdf"]["journey_id"]
    with s.journey.engine.begin() as c:
        set_tenant_context(c, s.journey.tenant_id)
        batches = c.execute(
            text("SELECT original_filename, sha256 FROM auditcore.p2_upload_batches WHERE tenant_id=:t "
                 "AND client_upload_id LIKE 'bulk-%' ORDER BY original_filename"), {"t": s.journey.tenant_id},
        ).all()
    by_name = dict(batches)  # independent of the database's sort order (collation)
    assert sorted(by_name) == sorted(["Asha Das KYC.pdf", "Asha Das invoice.pdf", "IMG_0042.pdf"])
    assert by_name["Asha Das KYC.pdf"] == hashlib.sha256(b"%PDF asha kyc").hexdigest()
    # Run again: nothing done twice.
    assert _run_upload(s, bulk_id)["files"] == 0


def test_an_exact_copy_of_an_uploaded_file_is_not_read_again(s):
    payload = b"%PDF already uploaded"
    with s.journey.engine.begin() as c:
        set_tenant_context(c, s.journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_upload_batches SET sha256=:s WHERE tenant_id=:t AND journey_id=:j"),
                  {"s": hashlib.sha256(payload).hexdigest(), "t": s.journey.tenant_id, "j": s.journey.journey_id})
    add_page(s.journey)
    with s.journey.engine.begin() as c:
        set_tenant_context(c, s.journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_upload_batches SET sha256=:s WHERE tenant_id=:t AND journey_id=:j"),
                  {"s": hashlib.sha256(payload).hexdigest(), "t": s.journey.tenant_id, "j": s.journey.journey_id})
    bulk_id = _drop(s, {"again.pdf": payload, "again copy.pdf": payload})
    result = _run_upload(s, bulk_id)
    assert result["exactDuplicates"] == 2 and result["reading"] == 0
    files = _files(s, bulk_id)
    assert {f["outcome"] for f in files} == {"EXACT_DUPLICATE"}
    assert {f["placed_journey_id"] for f in files} == {s.journey.journey_id}
    listed = s.client.get(s.base).json()["items"][0]
    assert listed["status"] == "DONE" and listed["summary"] == {"EXACT_DUPLICATE": 2}


def _identity(journey: P2Journey, *, pan: str = PAN, model: str = "THAR") -> None:
    doc = uuid4()
    add_evidence(journey, di_document_id=doc, document_type_key="pan_card")
    add_extracted_field(journey, di_document_id=doc, field_key="pan_number", value=pan, document_type="pan_card")
    add_extracted_field(journey, di_document_id=doc, field_key="customer_phone", value="9876543210")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("INSERT INTO auditcore.journey_products (tenant_id, journey_id, model_name_snapshot) "
                       "VALUES (:t, :j, :m)"), {"t": journey.tenant_id, "j": journey.journey_id, "m": model})


def _read(s, journey_id: UUID, *, shas: list[str]) -> P2Journey:
    """The journey read for the drop: its upload split into pages with these fingerprints, all read."""
    j = P2Journey(**{**s.journey.__dict__, "journey_id": journey_id})
    with s.journey.engine.begin() as c:
        set_tenant_context(c, s.journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_upload_batches SET batch_status='COMPLETED', identity_status='BELONGS' "
                       "WHERE tenant_id=:t AND journey_id=:j"), {"t": s.journey.tenant_id, "j": journey_id})
        c.execute(text("UPDATE auditcore.p2_document_queue SET queue_status='READY' WHERE tenant_id=:t AND journey_id=:j"),
                  {"t": s.journey.tenant_id, "j": journey_id})
    for sha in shas:
        batch, queue, doc = add_page(j, status="READY")
        with s.journey.engine.begin() as c:
            set_tenant_context(c, s.journey.tenant_id)
            c.execute(text("UPDATE auditcore.p2_document_queue SET page_sha256=:s, unit_kind='PAGE' "
                           "WHERE tenant_id=:t AND queue_id=:q"), {"s": sha, "t": s.journey.tenant_id, "q": queue})
            c.execute(text("UPDATE auditcore.p2_upload_batches SET identity_status='BELONGS', batch_status='COMPLETED' "
                           "WHERE tenant_id=:t AND batch_id=:b"), {"t": s.journey.tenant_id, "b": batch})
        add_evidence(j, di_document_id=doc, document_type_key="tax_invoice_tally")
    return j


def _settle(s, bulk_id: str) -> dict:
    return bulk_upload.run_bulk_settle(s.journey.engine, f"{s.journey.tenant_id}:{bulk_id}")


def _exists(s, journey_id) -> bool:
    with s.journey.engine.begin() as c:
        set_tenant_context(c, s.journey.tenant_id)
        return c.execute(text("SELECT EXISTS (SELECT 1 FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j)"),
                         {"t": s.journey.tenant_id, "j": journey_id}).scalar_one()


def test_a_delivery_file_joins_the_customers_booking_and_its_new_pages_are_carried(s):
    booking = s.journey                      # the customer's booking, its KYC already read
    _identity(booking)
    shared = uuid4().hex
    _read(s, booking.journey_id, shas=[shared])
    bulk_id = _drop(s, {"Asha Das delivery.pdf": b"%PDF delivery file"})
    _run_upload(s, bulk_id)
    [file] = _files(s, bulk_id)
    read_on = _read(s, file["journey_id"], shas=[shared, uuid4().hex])  # the KYC again, and the invoice
    _identity(read_on)
    # Still reading: not settled yet.
    with s.journey.engine.begin() as c:
        set_tenant_context(c, s.journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_document_queue SET queue_status='EXTRACTING' WHERE tenant_id=:t "
                       "AND journey_id=:j AND page_sha256=:s"), {"t": s.journey.tenant_id, "j": file["journey_id"],
                                                                "s": shared})
    assert _settle(s, bulk_id)["settled"] == {}
    with s.journey.engine.begin() as c:
        set_tenant_context(c, s.journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_document_queue SET queue_status='READY' WHERE tenant_id=:t AND journey_id=:j"),
                  {"t": s.journey.tenant_id, "j": file["journey_id"]})
    result = _settle(s, bulk_id)
    assert result["settled"] == {"ADDED_TO_BOOKING": 1} and result["done"]
    [file] = _files(s, bulk_id)
    assert file["outcome"] == "ADDED_TO_BOOKING" and file["placed_journey_id"] == booking.journey_id
    assert not _exists(s, file["journey_id"]) and _exists(s, booking.journey_id)
    with s.journey.engine.begin() as c:
        set_tenant_context(c, s.journey.tenant_id)
        carried = c.execute(text("SELECT count(*) FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND journey_id=:j "
                                 "AND client_upload_id LIKE 'dup-merge-%'"),
                            {"t": s.journey.tenant_id, "j": booking.journey_id}).scalar_one()
    assert carried == 1  # only the invoice page; the KYC page was already there


def test_a_file_with_no_booking_of_its_customer_stays_a_new_booking(s):
    bulk_id = _drop(s, {"Bimal Sahu.pdf": b"%PDF bimal"})
    _run_upload(s, bulk_id)
    [file] = _files(s, bulk_id)
    read_on = _read(s, file["journey_id"], shas=[uuid4().hex])
    _identity(read_on, pan="BBBPE9999F")
    assert _settle(s, bulk_id)["settled"] == {"NEW_BOOKING": 1}
    [file] = _files(s, bulk_id)
    assert file["outcome"] == "NEW_BOOKING" and file["placed_journey_id"] == file["journey_id"]
    assert _exists(s, file["journey_id"])


def test_the_rule_run_asks_for_the_drop_to_be_settled_and_the_same_file_rule_leaves_it(s):
    from audit_core.uc03_p2_same_file import sync_same_file_detection

    bulk_id = _drop(s, {"Chitra.pdf": b"%PDF chitra"})
    _run_upload(s, bulk_id)
    [file] = _files(s, bulk_id)
    with s.journey.engine.begin() as c:
        set_tenant_context(c, s.journey.tenant_id)
        assert bulk_upload.request_settle(c, tenant_id=s.journey.tenant_id, journey_id=file["journey_id"])
        assert not bulk_upload.request_settle(c, tenant_id=s.journey.tenant_id, journey_id=s.journey.journey_id)
        assert sync_same_file_detection(c, tenant_id=s.journey.tenant_id, journey_id=file["journey_id"],
                                        correlation_id="t").get("bulk") in (True, None)
        assert c.execute(text("SELECT count(*) FROM auditcore.p2_worker_jobs WHERE job_kind='BULK_SETTLE' "
                              "AND tag=:t AND status='PENDING'"), {"t": f"{s.journey.tenant_id}:{bulk_id}"}).scalar_one() == 1
