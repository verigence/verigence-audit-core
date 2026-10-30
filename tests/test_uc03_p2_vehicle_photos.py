"""P2 vehicle photos: direct upload, idempotent finalize, no DI, delivery gate."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import AllowAllAuthorization, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_p2_storage
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_task_producer import sync_vehicle_photo_task


class PhotoStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def presign_put(self, key: str, *, content_type: str, expires_seconds: int = 900) -> str:
        return f"https://storage.test/put/{key}"

    def presign_get(self, key: str, *, expires_seconds: int = 1800) -> str:
        return f"https://storage.test/get/{key}"

    def head_object(self, key: str) -> dict[str, object]:
        if key not in self.objects:
            raise uc03_p2_storage.P2DocumentStorageError("missing")
        return {"contentLength": len(self.objects[key]), "contentType": "image/jpeg", "etag": "x"}


@pytest.fixture
def journey(monkeypatch):
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2photo")
    storage = PhotoStorage()
    monkeypatch.setattr(uc03_p2_storage, "get_p2_document_storage", lambda: storage)
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        yield created, storage
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _upload(client, base, storage, client_upload_id, view="FRONT"):
    intents = client.post(f"{base}:upload-intents", json={"files": [
        {"clientUploadId": client_upload_id, "filename": "front.jpg", "contentType": "image/jpeg",
         "sizeBytes": 4, "viewCode": view}]})
    assert intents.status_code == 200, intents.text
    [upload] = intents.json()["uploads"]
    if not upload["alreadyStored"]:
        storage.objects[upload["uploadUrl"].split("/put/", 1)[1]] = b"jpeg"
    return client.post(f"{base}:finalize", json={
        "clientUploadId": client_upload_id, "filename": "front.jpg", "contentType": "image/jpeg", "viewCode": view})


def test_upload_is_idempotent_and_never_touches_di(journey):
    created, storage = journey
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{created.tenant_id}/journeys/{created.journey_id}/vehicle-photos"

    # Finalize before the object exists is a retryable conflict.
    early = client.post(f"{base}:finalize", json={
        "clientUploadId": "photo-early-0001", "filename": "a.jpg", "contentType": "image/jpeg"})
    assert early.status_code == 409

    first = _upload(client, base, storage, "photo-front-0001")
    assert first.status_code == 200, first.text
    again = client.post(f"{base}:finalize", json={
        "clientUploadId": "photo-front-0001", "filename": "front.jpg", "contentType": "image/jpeg"})
    assert again.json()["photoId"] == first.json()["photoId"]
    retry_intent = client.post(f"{base}:upload-intents", json={"files": [
        {"clientUploadId": "photo-front-0001", "filename": "front.jpg", "contentType": "image/jpeg", "sizeBytes": 4}]})
    assert retry_intent.json()["uploads"][0]["alreadyStored"] is True

    listing = client.get(base).json()
    assert [p["viewCode"] for p in listing["photos"]] == ["FRONT"]
    assert listing["photos"][0]["url"].startswith("https://storage.test/get/")

    with created.engine.begin() as connection:
        set_tenant_context(connection, created.tenant_id)
        queued = connection.execute(
            text("SELECT COUNT(*) FROM auditcore.p2_document_queue WHERE tenant_id=:t"),
            {"t": created.tenant_id},
        ).scalar_one()
        evidence = connection.execute(
            text("SELECT COUNT(*) FROM auditcore.evidence WHERE tenant_id=:t"), {"t": created.tenant_id},
        ).scalar_one()
    assert queued == 0 and evidence == 0

    assert client.delete(f"{base}/{first.json()['photoId']}").status_code == 204
    assert client.get(base).json()["photos"] == []
    refused = client.post(f"{base}:upload-intents", json={"files": [
        {"clientUploadId": "photo-x-0001", "filename": "a.docx", "contentType": "application/msword",
         "sizeBytes": 4}]})
    assert refused.status_code == 422 and "photos (JPEG, PNG, WEBP or HEIC) or PDF" in refused.json()["detail"]


def test_delivery_photo_task_raises_and_closes_itself(journey):
    created, storage = journey
    with created.engine.begin() as connection:
        set_tenant_context(connection, created.tenant_id)
        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_journey_runtime (tenant_id, journey_id, current_stage)
                VALUES (:t, :j, 'DELIVERY_DOCUMENT_UPLOAD')
                """
            ),
            {"t": created.tenant_id, "j": created.journey_id},
        )
        # no task while other delivery documents are still missing
        assert sync_vehicle_photo_task(connection, tenant_id=created.tenant_id,
                                       journey_id=created.journey_id) is None
        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_stage_gate_state (tenant_id, journey_id, stage_code, gate_key,
                    gate_status, details, evaluated_at_utc)
                VALUES (:t, :j, 'DELIVERY', 'REQUIRED_DOCUMENTS', 'PASS', '{}'::jsonb, now())
                """
            ),
            {"t": created.tenant_id, "j": created.journey_id},
        )
        assert sync_vehicle_photo_task(connection, tenant_id=created.tenant_id,
                                       journey_id=created.journey_id) == "RAISED"
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{created.tenant_id}/journeys/{created.journey_id}/vehicle-photos"
    assert _upload(client, base, storage, "photo-rear-0001", view="REAR").status_code == 200
    with created.engine.begin() as connection:
        set_tenant_context(connection, created.tenant_id)
        assert sync_vehicle_photo_task(connection, tenant_id=created.tenant_id,
                                       journey_id=created.journey_id) == "VERIFIED"
        status = connection.execute(
            text("SELECT task_status FROM auditcore.p2_tasks WHERE tenant_id=:t AND task_type="
                 "'DELIVERY_VEHICLE_PHOTOS_MISSING'"), {"t": created.tenant_id},
        ).scalar_one()
    assert status == "VERIFIED_COMPLETE"


# ── PDFs (product owner 2026-10-30) ──────────────────────────────────────────

def _pdf(client, base, storage, client_upload_id, *, size=4):
    intents = client.post(f"{base}:upload-intents", json={"files": [
        {"clientUploadId": client_upload_id, "filename": f"{client_upload_id}.pdf",
         "contentType": "application/pdf", "sizeBytes": size}]})
    if intents.status_code != 200:
        return intents
    [upload] = intents.json()["uploads"]
    if not upload["alreadyStored"]:
        storage.objects[upload["uploadUrl"].split("/put/", 1)[1]] = b"x" * size
    return client.post(f"{base}:finalize", json={
        "clientUploadId": client_upload_id, "filename": f"{client_upload_id}.pdf", "contentType": "application/pdf"})


def test_a_pc_can_add_up_to_three_pdfs_and_a_fourth_is_refused_with_a_reason(journey):
    created, storage = journey
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{created.tenant_id}/journeys/{created.journey_id}/vehicle-photos"
    for n in range(3):
        added = _pdf(client, base, storage, f"photo-pdf-000{n}")
        assert added.status_code == 200, added.text
        assert added.json()["contentType"] == "application/pdf" and added.json()["url"].endswith(".pdf")
    fourth = _pdf(client, base, storage, "photo-pdf-0003")
    assert fourth.status_code == 422
    assert fourth.json()["detail"] == ("At most 3 PDFs can be added per booking (3 already added). "
                                       "Remove one to add another.")
    # Photos are not limited by the PDFs.
    assert _upload(client, base, storage, "photo-front-0009").status_code == 200
    listing = client.get(base).json()
    assert listing["pdfLimit"] == 3 and listing["maxPdfBytes"] == 50 * 1024 * 1024
    assert sum(p["contentType"] == "application/pdf" for p in listing["photos"]) == 3
    # Removing one makes room again.
    first_pdf = next(p for p in listing["photos"] if p["contentType"] == "application/pdf")
    assert client.delete(f"{base}/{first_pdf['photoId']}").status_code == 204
    assert _pdf(client, base, storage, "photo-pdf-0003").status_code == 200


def test_a_pdf_over_50_mb_is_refused_with_its_size(journey):
    created, _ = journey
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{created.tenant_id}/journeys/{created.journey_id}/vehicle-photos"
    big = client.post(f"{base}:upload-intents", json={"files": [
        {"clientUploadId": "photo-big-0001", "filename": "delivery.pdf", "contentType": "application/pdf",
         "sizeBytes": 63 * 1024 * 1024}]})
    assert big.status_code == 422
    assert big.json()["detail"].startswith("delivery.pdf is 63.0 MB. A PDF can be at most 50 MB")
    # Exactly 50 MB is allowed; a photo stays at 15 MB.
    assert client.post(f"{base}:upload-intents", json={"files": [
        {"clientUploadId": "photo-big-0002", "filename": "ok.pdf", "contentType": "application/pdf",
         "sizeBytes": 50 * 1024 * 1024}]}).status_code == 200
    photo = client.post(f"{base}:upload-intents", json={"files": [
        {"clientUploadId": "photo-big-0003", "filename": "car.jpg", "contentType": "image/jpeg",
         "sizeBytes": 20 * 1024 * 1024}]})
    assert photo.status_code == 422 and "A photo can be at most 15 MB" in photo.json()["detail"]


def test_the_stored_size_is_checked_again_when_the_pdf_is_saved(journey):
    """The size the browser declared is not trusted: finalize reads the object."""
    created, storage = journey
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{created.tenant_id}/journeys/{created.journey_id}/vehicle-photos"
    intents = client.post(f"{base}:upload-intents", json={"files": [
        {"clientUploadId": "photo-lie-0001", "filename": "lie.pdf", "contentType": "application/pdf",
         "sizeBytes": 4}]})
    key = intents.json()["uploads"][0]["uploadUrl"].split("/put/", 1)[1]
    storage.objects[key] = b"x" * (50 * 1024 * 1024 + 1)
    saved = client.post(f"{base}:finalize", json={
        "clientUploadId": "photo-lie-0001", "filename": "lie.pdf", "contentType": "application/pdf"})
    assert saved.status_code == 422 and "A PDF can be at most 50 MB" in saved.json()["detail"]
    assert client.get(base).json()["photos"] == []
