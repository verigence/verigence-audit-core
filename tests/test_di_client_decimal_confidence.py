import httpx

from audit_core.di_client import DiClient


def _success(data):
    return {"errorCode": "000", "errorMessage": "Success", "data": data}


def test_di_client_accepts_decimal_confidence_serialized_as_json_string() -> None:
    tenant = "tenant-1"
    context = "ctx-1"
    document_id = "22222222-2222-2222-2222-222222222222"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/documents/{document_id}"):
            return httpx.Response(
                200,
                json=_success(
                    {
                        "documentId": document_id,
                        "uploadStatus": "FIT",
                        "processingStatus": "PROCESSED",
                        "confirmationStatus": "CONFIRMED",
                        "confidenceScore": "96.50",
                    }
                ),
            )
        if request.url.path.endswith(f"/documents/{document_id}/fields"):
            return httpx.Response(
                200,
                json=_success(
                    {
                        "documentId": document_id,
                        "fields": [
                            {
                                "canonicalFieldId": "33333333-3333-3333-3333-333333333333",
                                "fieldKey": "customer_phone",
                                "currentValue": "9999999999",
                                "valueSource": "MACHINE",
                                "confidenceScore": "97.25",
                                "versionNo": 1,
                                "pageNo": 1,
                                "evidenceRegion": {
                                    "type": "BOX_2D",
                                    "coordinateSystem": "NORMALIZED_1000",
                                    "box": [100, 200, 140, 500],
                                },
                            }
                        ],
                    }
                ),
            )
        return httpx.Response(404)

    with DiClient(base_url="https://di.test", transport=httpx.MockTransport(handle)) as client:
        document = client.get_audit_document(
            token="token",
            tenant_id=tenant,
            external_context_ref=context,
            document_id=document_id,
        )
        facts = client.get_audit_document_facts(
            token="token",
            tenant_id=tenant,
            external_context_ref=context,
            document_id=document_id,
        )

    assert document.confidence_score == 96.5
    assert facts[0].confidence_score == 97.25
    assert facts[0].page_no == 1
    assert facts[0].evidence_region is not None
