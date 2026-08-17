from __future__ import annotations

import httpx
import pytest

from audit_core.di_client import DiClient, DiClientError

TENANT = "DummyTenant"
SUBJECT = "11111111-1111-1111-1111-111111111111"
DOCUMENT = "22222222-2222-2222-2222-222222222222"
TOKEN = "delegated-user-token"


def _envelope(data: dict | None, *, code: str = "000") -> dict:
    return {
        "errorCode": code,
        "errorMessage": "Success" if code == "000" else "not ready",
        "data": data,
    }


def test_client_accepts_new_di_envelope_and_slim_document_contract() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/subjects"):
            return httpx.Response(
                201,
                json=_envelope(
                    {
                        "tenantId": TENANT,
                        "subjectId": SUBJECT,
                        "subjectType": "OTHER",
                        "displayName": "Dummy Customer",
                        "status": "ACTIVE",
                    }
                ),
            )
        if request.method == "POST" and request.url.path.endswith("/documents"):
            return httpx.Response(
                201,
                json=_envelope(
                    {
                        "documentId": DOCUMENT,
                        "uploadStatus": "ACCEPTED",
                        "processingStatus": "PENDING",
                    }
                ),
            )
        if request.method == "GET" and request.url.path.endswith(DOCUMENT):
            return httpx.Response(
                200,
                json=_envelope(
                    {
                        "documentId": DOCUMENT,
                        "documentTypeKey": "booking_form",
                        "uploadStatus": "ACCEPTED",
                        "processingStatus": "PROCESSED",
                        "confirmationStatus": "CONFIRMED",
                        # DI DocumentData declares Decimal; Pydantic JSON emits
                        # this as a numeric string in the live API response.
                        "confidenceScore": "96.00",
                        "registeredAtUtc": "2026-08-16T10:00:00Z",
                    }
                ),
            )
        if request.method == "GET" and request.url.path.endswith("/fields"):
            return httpx.Response(
                200,
                json=_envelope(
                    {
                        "documentId": DOCUMENT,
                        "fields": [
                            {
                                "canonicalFieldId": "33333333-3333-3333-3333-333333333333",
                                "fieldKey": "customer_name",
                                "currentValue": "Dummy Customer",
                                "valueSource": "MACHINE",
                                "confidenceScore": 97.0,
                                "versionNo": 1,
                            }
                        ],
                    }
                ),
            )
        return httpx.Response(404)

    with DiClient(base_url="https://di.test", transport=httpx.MockTransport(handle)) as client:
        subject = client.create_subject(
            token=TOKEN,
            tenant_id=TENANT,
            subject_type="OTHER",
            display_name="Dummy Customer",
        )
        uploaded = client.upload_document(
            token=TOKEN,
            tenant_id=TENANT,
            subject_id=SUBJECT,
            filename="booking_form.pdf",
            content=b"pdf",
            content_type="application/pdf",
            source_channel="API",
            document_type_key="booking_form",
        )
        document = client.get_document(
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

    assert subject.subject_id == SUBJECT
    assert uploaded.subject_id == SUBJECT
    assert uploaded.processing_status == "PENDING"
    assert uploaded.confirmation_status is None
    assert uploaded.verification_state is None
    assert document.processing_status == "PROCESSED"
    assert document.confirmation_status == "CONFIRMED"
    assert document.confidence_score == 96.0
    assert document.correlation_id is None
    assert facts[0].field_key == "customer_name"
    assert facts[0].value == "Dummy Customer"


def test_client_maps_success_http_with_di_error_envelope_to_dependency_error() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_envelope(None, code="E008"))
    )
    with (
        DiClient(base_url="https://di.test", transport=transport) as client,
        pytest.raises(DiClientError) as raised,
    ):
        client.get_document_facts(
            token=TOKEN,
            tenant_id=TENANT,
            subject_id=SUBJECT,
            document_id=DOCUMENT,
        )

    assert raised.value.status_code == 409
    assert raised.value.code == "DI_E008"
    assert raised.value.retryable is False
