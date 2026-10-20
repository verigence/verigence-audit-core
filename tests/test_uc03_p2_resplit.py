"""A file that came in through the old capture screen as ONE document is split
into pages under the same Journey, without anyone uploading it again
(2026-10-20, journey 9d1c307c: a 5-page booking PDF read as one page)."""
from __future__ import annotations

import io
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    MemoryStorage,
    add_evidence,
    create_p2_journey,
    database_engine,
)
from pypdf import PdfWriter
from sqlalchemy import text

from audit_core import uc03_p2_resplit as resplit
from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_document_capture_v2 import get_di_client, get_security_oauth_client


def _pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for n in range(pages):
        writer.add_blank_page(width=200 + n, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class _Security:
    def get_service_token(self, *, audience: str) -> str:
        return "token"


class _Di:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def get_audit_document_content(self, **_: object) -> tuple[bytes, str, None]:
        return self.payload, "application/pdf", None


@pytest.fixture
def journey(monkeypatch):
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2split")
    storage = MemoryStorage()
    monkeypatch.setattr(resplit, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(resplit, "_ensure_di_context", lambda **_: ("ctx", "token"))
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    app.dependency_overrides[get_security_oauth_client] = lambda: _Security()
    app.dependency_overrides[get_di_client] = lambda: _Di(_pdf(5))
    created.storage = storage  # type: ignore[attr-defined]
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        with engine.begin() as connection:
            set_tenant_context(connection, created.tenant_id)
            connection.execute(text("DELETE FROM auditcore.p2_document_queue WHERE tenant_id=:t"),
                               {"t": created.tenant_id})
            connection.execute(text("DELETE FROM auditcore.p2_upload_batches WHERE tenant_id=:t"),
                               {"t": created.tenant_id})
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _base(journey) -> str:
    return f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"


def test_an_old_screen_file_is_listed_split_into_pages_and_replaces_the_whole_file(journey):
    document_id = uuid4()
    evidence_id = add_evidence(journey, di_document_id=document_id, document_type_key="booking_form")
    client = TestClient(app)

    listed = client.get(f"{_base(journey)}/old-screen-documents").json()["documents"]
    assert [d["documentId"] for d in listed] == [str(document_id)]

    response = client.post(f"{_base(journey)}/documents/{document_id}:split-pages")
    assert response.status_code == 202, response.text
    batch_id = UUID(response.json()["batchId"])

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        batch = connection.execute(
            text("SELECT batch_status, replaces_document_id, replaces_evidence_id, size_bytes, original_object_key "
                 "FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND batch_id=:b"),
            {"t": journey.tenant_id, "b": batch_id},
        ).mappings().one()
        queued = connection.execute(
            text("SELECT count(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND work_type='SPLIT_BATCH' "
                 "AND work_key=:k"),
            {"t": journey.tenant_id, "k": str(batch_id)},
        ).scalar_one()
    assert batch["batch_status"] == "UPLOADED" and queued == 1
    assert batch["replaces_document_id"] == document_id and batch["replaces_evidence_id"] == evidence_id
    assert journey.storage.objects[batch["original_object_key"]] == _pdf(5)

    lease = uuid4()
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        work_id = connection.execute(
            text("UPDATE auditcore.p2_work_queue SET lease_token=:l WHERE tenant_id=:t AND work_type='SPLIT_BATCH' "
                 "AND work_key=:k RETURNING work_id"),
            {"l": lease, "t": journey.tenant_id, "k": str(batch_id)},
        ).scalar_one()
    work = worker.WorkItem(journey.tenant_id, work_id, journey.journey_id, "SPLIT_BATCH", str(batch_id), {}, 0,
                           None, None, lease)
    worker._split_batch(journey.engine, work)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        pages = connection.execute(
            text("SELECT count(*) FROM auditcore.p2_document_queue WHERE tenant_id=:t AND batch_id=:b"),
            {"t": journey.tenant_id, "b": batch_id},
        ).scalar_one()
    assert pages == 5

    # Already being split: not offered again, and a second request is refused.
    assert client.get(f"{_base(journey)}/old-screen-documents").json()["documents"] == []
    assert client.post(f"{_base(journey)}/documents/{document_id}:split-pages").status_code == 409


def test_a_document_already_read_page_by_page_cannot_be_split(journey):
    client = TestClient(app)
    response = client.post(f"{_base(journey)}/documents/{uuid4()}:split-pages")
    assert response.status_code == 409
