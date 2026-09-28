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
