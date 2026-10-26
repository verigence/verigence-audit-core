"""A Team Lead deletes a journey (product owner, 2026-10-23).

Real Postgres; Document Intelligence, Security's token client and R2 are
doubles that record what they were asked to remove.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    AllowAllAuthorization,
    add_page,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text
from starlette.requests import Request

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.di_capture_v2_client import DiCaptureV2Error
from audit_core.errors import (
    BusinessValidationError,
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
)
from audit_core.evidence import _external_context_ref
from audit_core.uc03_p2_journey_delete import (
    DeleteJourneyCommand,
    delete_journey,
    storage_prefixes,
)
from audit_core.uc03_p2_storage import P2DocumentStorageError

REASON = "Uploaded against the wrong dealer by mistake"


class FakeSecurity:
    def get_service_token(self, *, audience: str) -> str:
        assert audience == "di"
        return "service-token"


class FakeDi:
    def __init__(self, *, fail_on: str | None = None, missing: set[str] | None = None) -> None:
        self.deleted: list[tuple[str, str, str]] = []
        self.fail_on = fail_on
        self.missing = missing or set()

    def delete_document(self, *, token: str, tenant_id: str, external_context_ref: str, document_id: str) -> None:
        assert token == "service-token"
        if document_id == self.fail_on:
            raise DiCaptureV2Error(status_code=500, detail="boom")
        if document_id in self.missing:
            raise DiCaptureV2Error(status_code=404, detail="gone")
        self.deleted.append((tenant_id, external_context_ref, document_id))


class FakeStorage:
    def __init__(self, listed: dict[str, list[str]] | None = None, *, fail: bool = False) -> None:
        self.listed = listed or {}
        self.deleted: list[str] = []
        self.fail = fail

    def list_keys(self, prefix: str) -> list[str]:
        return list(self.listed.get(prefix, []))

    def delete_objects(self, keys: list[str]) -> int:
        if self.fail:
            raise P2DocumentStorageError("down")
        self.deleted.extend(keys)
        return len(keys)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2del")
    tl = f"tl-{uuid4().hex[:8]}"
    with engine.begin() as connection:
        set_tenant_context(connection, created.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id, outlet_id) VALUES (:t, :a, 'TL', :d, :o)"),
            {"t": created.tenant_id, "a": tl, "d": created.dealer_id, "o": created.outlet_id},
        )
    created.tl = tl
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _run(journey, *, actor=None, reason=REASON, di=None, storage=None):
    return delete_journey(
        tenant_id=journey.tenant_id,
        journey_id=journey.journey_id,
        command=DeleteJourneyCommand(reason=reason),
        request=_request(),
        human_principal=SimpleNamespace(subject=actor or journey.tl),
        authorization_client=AllowAllAuthorization(role_key="TL"),
        engine=journey.engine,
        security_client=FakeSecurity(),
        v2_client=di or FakeDi(),
        storage=storage or FakeStorage(),
    )


def _count(journey, sql: str, **params) -> int:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params}).scalar_one()


def _audit(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT * FROM auditcore.p2_journey_deletions WHERE tenant_id=:t AND journey_id=:j "
                 "ORDER BY requested_at_utc"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().all()


def _mark_reviewed(journey, stage="BOOKING"):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_stage_document_reviews (tenant_id, journey_id, stage_code, "
                 "reviewed_by_actor_id, reviewed_by_role_code, document_count, dealer_id, outlet_id) "
                 "VALUES (:t, :j, :s, 'rev', 'REVIEWER', 1, :d, :o)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "s": stage, "d": journey.dealer_id,
             "o": journey.outlet_id},
        )


def test_deleting_removes_the_journey_its_documents_and_files_and_keeps_an_audit_record(journey):
    batch_id, _queue, di_document = add_page(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.delivery_vehicle_photos (tenant_id, journey_id, object_key, "
                 "original_filename, content_type, size_bytes, uploaded_by_actor_id) "
                 "VALUES (:t, :j, :k, 'car.jpg', 'image/jpeg', 10, 'pc')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": "photo-key-1"},
        )
    pages_prefix, photos_prefix = storage_prefixes(journey.tenant_id, journey.journey_id)
    storage = FakeStorage({pages_prefix: [f"{pages_prefix}{batch_id}/pages/0001-x.pdf"],
                           photos_prefix: [f"{photos_prefix}abc.jpg", "photo-key-1"]})
    di = FakeDi()

    result = _run(journey, di=di, storage=storage)

    # Document Intelligence: the one page document, under this journey's own context.
    context = _external_context_ref(journey_id=journey.journey_id, customer_id=journey.customer_id)
    assert di.deleted == [(journey.tenant_id, context, str(di_document))]
    assert result.diDocumentsDeleted == 1
    # R2: the recorded keys and the prefix listings, each once.
    assert sorted(storage.deleted) == sorted({
        f"p2/{batch_id}", f"p2/{batch_id}/1", "photo-key-1",
        f"{pages_prefix}{batch_id}/pages/0001-x.pdf", f"{photos_prefix}abc.jpg",
    })
    assert result.storageObjectsDeleted == len(storage.deleted)
    # Audit Core: the journey and its rows are gone.
    assert _count(journey, "SELECT count(*) FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j") == 0
    assert _count(journey, "SELECT count(*) FROM auditcore.p2_document_queue WHERE tenant_id=:t AND journey_id=:j") == 0
    assert _count(journey, "SELECT count(*) FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND journey_id=:j") == 0
    assert _count(journey, "SELECT count(*) FROM auditcore.delivery_vehicle_photos WHERE tenant_id=:t AND journey_id=:j") == 0
    # The record that stays.
    (record,) = _audit(journey)
    assert record["deletion_status"] == "COMPLETED"
    assert record["reason"] == REASON
    assert record["deleted_by_actor_id"] == journey.tl and record["deleted_by_role"] == "TL"
    assert record["journey_reference"] == result.journeyReference
    assert record["customer_name"] == "P2 Customer"
    assert record["dealer_name"] == "D" and record["outlet_name"] == "O"
    assert record["document_count"] == 1
    assert record["di_documents_deleted"] == 1 and record["r2_objects_deleted"] == len(storage.deleted)
    assert record["completed_at_utc"] is not None and record["failure_detail"] is None


def test_a_document_digital_intelligence_no_longer_has_is_not_a_failure(journey):
    _batch, _queue, di_document = add_page(journey)
    result = _run(journey, di=FakeDi(missing={str(di_document)}))
    assert result.diDocumentsDeleted == 1
    assert _count(journey, "SELECT count(*) FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j") == 0


def test_only_a_team_lead_may_delete(journey):
    di = FakeDi()
    with pytest.raises(AuthorizationError) as caught:
        _run(journey, actor=journey.actor_id, di=di)  # the PC who owns the journey
    assert caught.value.status_code == 403
    assert di.deleted == []
    assert _count(journey, "SELECT count(*) FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j") == 1
    assert _audit(journey) == []


def test_a_reason_is_required(journey):
    with pytest.raises(BusinessValidationError):
        _run(journey, reason="   too short ")
    assert _count(journey, "SELECT count(*) FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j") == 1
    assert _audit(journey) == []


def test_a_reviewed_journey_cannot_be_deleted(journey):
    _mark_reviewed(journey, "DELIVERY")
    di, storage = FakeDi(), FakeStorage()
    with pytest.raises(ConflictError) as caught:
        _run(journey, di=di, storage=storage)
    assert "Delivery" in caught.value.detail and "reviewed" in caught.value.detail
    assert di.deleted == [] and storage.deleted == []
    assert _count(journey, "SELECT count(*) FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j") == 1
    assert _audit(journey) == []


def test_a_closed_journey_cannot_be_deleted(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, business_status) "
                 "VALUES (:t, :j, 'BOOKING', 'BOOKING_CANCELLED')"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
    with pytest.raises(ConflictError) as caught:
        _run(journey)
    assert "closed" in caught.value.detail
    assert _count(journey, "SELECT count(*) FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j") == 1


def test_a_journey_outside_the_callers_scope_is_not_found(journey):
    with pytest.raises(NotFoundError):
        _run(journey, actor=f"stranger-{uuid4().hex[:6]}")


def test_a_document_intelligence_failure_keeps_the_journey_and_records_the_attempt(journey):
    _batch, _queue, di_document = add_page(journey)
    storage = FakeStorage()
    with pytest.raises(DependencyUnavailableError):
        _run(journey, di=FakeDi(fail_on=str(di_document)), storage=storage)
    assert storage.deleted == []
    assert _count(journey, "SELECT count(*) FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j") == 1
    (record,) = _audit(journey)
    assert record["deletion_status"] == "FAILED"
    assert "Document Intelligence" in record["failure_detail"]

    # The Team Lead deletes again and it goes through; both attempts stay on file.
    _run(journey)
    assert _count(journey, "SELECT count(*) FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j") == 0
    assert [r["deletion_status"] for r in _audit(journey)] == ["FAILED", "COMPLETED"]


def test_a_storage_failure_keeps_the_journey_and_records_the_attempt(journey):
    add_page(journey)
    with pytest.raises(DependencyUnavailableError):
        _run(journey, storage=FakeStorage(fail=True))
    assert _count(journey, "SELECT count(*) FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j") == 1
    (record,) = _audit(journey)
    assert record["deletion_status"] == "FAILED" and "storage" in record["failure_detail"]


def test_storage_prefixes_end_with_a_slash_so_one_journey_never_matches_another():
    tenant, one = "t1", uuid4()
    for prefix in storage_prefixes(tenant, one):
        assert prefix.endswith("/") and str(one) in prefix


def test_deleting_also_sweeps_the_dated_folder_an_upload_sits_in(journey):
    batch_id, _queue, _doc = add_page(journey)
    dated = f"p2-documents/{journey.tenant_id}/2026-W44/2026-10-26/{journey.journey_id}/{batch_id}/original/o__p__f.pdf"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_upload_batches SET original_object_key=:k WHERE tenant_id=:t AND batch_id=:b"),
            {"k": dated, "t": journey.tenant_id, "b": batch_id},
        )
    folder = f"p2-documents/{journey.tenant_id}/2026-W44/2026-10-26/{journey.journey_id}/"
    straggler = f"{folder}{batch_id}/pages/0009-left-behind.pdf"
    storage = FakeStorage({folder: [straggler]})

    _run(journey, storage=storage)

    assert dated in storage.deleted and straggler in storage.deleted
