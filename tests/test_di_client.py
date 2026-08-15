from __future__ import annotations

import httpx
import pytest

from audit_core.di_client import (
    DiClient,
    DiClientError,
    DiDocument,
    DiFact,
    DiSubject,
    DiVerification,
)

TENANT = "tenant-1"
SUBJECT = "11111111-1111-1111-1111-111111111111"
DOCUMENT = "22222222-2222-2222-2222-222222222222"
TOKEN = "downstream-token"


def _document_payload() -> dict:
    return {
        "tenantId": TENANT,
        "documentId": DOCUMENT,
        "subjectId": SUBJECT,
        "sourceChannel": "WEB",
        "uploadStatus": "FIT",
        "processingStatus": "PROCESSED",
        "confirmationStatus": "CONFIRMED",
        "confidenceScore": 94.5,
        "verificationThresholdApplied": 90,
        "humanVerificationStatus": "OPTIONAL",
        "verificationState": "NOT_VERIFIED",
        "contentState": "AVAILABLE",
        "correlationId": "corr-1",
        "registeredAtUtc": "2026-08-15T10:00:00Z",
    }


def test_client_maps_subject_upload_status_facts_and_verification() -> None:
    seen: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        seen.append((request.method, request.url.path))

        if request.method == "POST" and request.url.path == f"/v1/tenants/{TENANT}/subjects":
            return httpx.Response(
                201,
                json={
                    "tenantId": TENANT,
                    "subjectId": SUBJECT,
                    "subjectType": "PERSON",
                    "displayName": "Customer",
                    "status": "ACTIVE",
                    "createdAtUtc": "2026-08-15T10:00:00Z",
                    "updatedAtUtc": "2026-08-15T10:00:00Z",
                },
            )
        if (
            request.method == "POST"
            and request.url.path == f"/v1/tenants/{TENANT}/subjects/{SUBJECT}/documents"
        ):
            assert b'name="sourceChannel"' in request.content
            assert b"WEB" in request.content
            assert b"booking.pdf" in request.content
            return httpx.Response(201, json=_document_payload())
        if (
            request.method == "GET"
            and request.url.path
            == f"/v1/tenants/{TENANT}/subjects/{SUBJECT}/documents/{DOCUMENT}"
        ):
            return httpx.Response(200, json=_document_payload())
        if request.method == "GET" and request.url.path.endswith(f"/{DOCUMENT}/fields"):
            return httpx.Response(
                200,
                json={
                    "documentId": DOCUMENT,
                    "fields": [
                        {
                            "canonicalFieldId": "33333333-3333-3333-3333-333333333333",
                            "fieldKey": "invoice_number",
                            "currentValue": "INV-42",
                            "valueSource": "MACHINE",
                            "confidenceScore": 98.1,
                            "versionNo": 1,
                            "acceptedAt": "2026-08-15T10:01:00Z",
                        }
                    ],
                },
            )
        if request.method == "POST" and request.url.path.endswith(f"/{DOCUMENT}/verification"):
            return httpx.Response(
                201,
                json={
                    "verificationId": "44444444-4444-4444-4444-444444444444",
                    "documentId": DOCUMENT,
                    "verifiedAt": "2026-08-15T10:02:00+00:00",
                    "verifiedByActorId": "tl-1",
                    "remarks": "checked",
                    "fieldCorrectionCount": 0,
                },
            )
        return httpx.Response(404)

    with DiClient(
        base_url="https://di.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        subject = client.create_subject(
            token=TOKEN,
            tenant_id=TENANT,
            subject_type="PERSON",
            display_name="Customer",
        )
        uploaded = client.upload_document(
            token=TOKEN,
            tenant_id=TENANT,
            subject_id=SUBJECT,
            filename="booking.pdf",
            content=b"pdf-bytes",
            content_type="application/pdf",
            source_channel="WEB",
        )
        status = client.get_document(
            token=TOKEN,
            tenant_id=TENANT,
            subject_id=SUBJECT,
            document_id=DOCUMENT,
        )
        facts = client.get_document_facts(
            token=TOKEN,
            tenant_id=TENANT,
            subject_id=SUBJECT,
            document_id=DOCUMENT,
        )
        verification = client.verify_document(
            token=TOKEN,
            tenant_id=TENANT,
            subject_id=SUBJECT,
            document_id=DOCUMENT,
            remarks="checked",
        )

    assert subject == DiSubject(subject_id=SUBJECT, status="ACTIVE")
    assert isinstance(uploaded, DiDocument)
    assert uploaded.document_id == DOCUMENT
    assert uploaded.processing_status == "PROCESSED"
    assert status.confirmation_status == "CONFIRMED"
    assert facts == (
        DiFact(
            canonical_field_id="33333333-3333-3333-3333-333333333333",
            field_key="invoice_number",
            value="INV-42",
            value_source="MACHINE",
            confidence_score=98.1,
            version_no=1,
        ),
    )
    assert verification == DiVerification(
        verification_id="44444444-4444-4444-4444-444444444444",
        document_id=DOCUMENT,
        verified_at="2026-08-15T10:02:00+00:00",
        verified_by_actor_id="tl-1",
        field_correction_count=0,
    )
    assert len(seen) == 5


def test_client_translates_di_problem_without_exposing_wire_body() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "code": "STORAGE_UNAVAILABLE",
                "title": "provider detail that callers must not branch on",
                "status": 503,
                "retryable": True,
            },
        )

    with DiClient(base_url="https://di.test", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DiClientError) as raised:
            client.get_document(
                token=TOKEN,
                tenant_id=TENANT,
                subject_id=SUBJECT,
                document_id=DOCUMENT,
            )

    assert raised.value.status_code == 503
    assert raised.value.code == "STORAGE_UNAVAILABLE"
    assert raised.value.retryable is True
    assert "provider detail" not in str(raised.value)


def test_client_rejects_malformed_di_success_contract() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"unexpected": True}))

    with DiClient(base_url="https://di.test", transport=transport) as client:
        with pytest.raises(DiClientError) as raised:
            client.get_document(
                token=TOKEN,
                tenant_id=TENANT,
                subject_id=SUBJECT,
                document_id=DOCUMENT,
            )

    assert raised.value.status_code == 502
    assert raised.value.code == "DI_CONTRACT_ERROR"
    assert raised.value.retryable is False
