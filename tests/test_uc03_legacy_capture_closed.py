"""The Phase 1 create-booking and whole-file upload are closed (2026-10-20).

Found live 19 Oct (journey 9d1c307c): the old Web Overview's "Capture new
booking" created the Journey through POST /v1/.../uc03/bookings and uploaded
Ramakanta_Sahoo.pdf (5 pages) through POST /v2/.../uc03/documents/
upload-intents. Document Intelligence read the whole file as ONE booking form;
the PAN and Aadhaar inside it were never read and the customer was never
named. Booking & Delivery (P2) splits every PDF into pages first and is now
the only way in. The refusal runs before anything else on the route: no
authentication, database or Document Intelligence call is reached.
"""
from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from audit_core.main import app
from audit_core.uc03_legacy_capture_guard import LEGACY_CAPTURE_ERROR_CODE


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_old_create_booking_is_refused() -> None:
    response = _client().post(
        "/v1/tenants/tenant-x/uc03/bookings",
        headers={"Idempotency-Key": "closed-create-0001"},
        json={"outletId": str(uuid4())},
    )
    assert response.status_code == 410, response.text
    body = response.json()
    assert body["errorCode"] == LEGACY_CAPTURE_ERROR_CODE
    assert "Booking & Delivery" in body["detail"]


def test_old_whole_file_upload_is_refused() -> None:
    response = _client().post(
        f"/v2/tenants/tenant-x/journeys/{uuid4()}/uc03/documents/upload-intents",
        json={"files": [{"clientUploadId": "c-1", "filename": "Ramakanta_Sahoo.pdf", "contentType": "application/pdf"}]},
    )
    assert response.status_code == 410, response.text
    body = response.json()
    assert body["errorCode"] == LEGACY_CAPTURE_ERROR_CODE
    assert "page by page" in body["detail"]
