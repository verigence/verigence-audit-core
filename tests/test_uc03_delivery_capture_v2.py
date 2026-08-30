from uuid import uuid4

from audit_core.uc03_delivery_capture_v2 import _build_delivery_capture_response


def test_delivery_capture_never_blocks_business_process_when_required_document_missing() -> None:
    response = _build_delivery_capture_response(
        journey_id=uuid4(),
        context_ref="test",
        requirements=[
            {
                "requirement_key": "TAX_INVOICE",
                "document_type_key": "tax_invoice",
                "requirement_level": "REQUIRED",
                "requirement_status": "PENDING",
                "display_label": "Tax Invoice",
                "condition_key": None,
            }
        ],
        audit_documents=[],
        di_documents=[],
        submitted=False,
    )

    assert response.canSubmit is True
    assert response.requirements[0].state == "NOT_UPLOADED"
    assert response.requirements[0].blocksContinue is False


def test_delivery_capture_links_classified_document_without_creating_gate() -> None:
    document_id = uuid4()
    response = _build_delivery_capture_response(
        journey_id=uuid4(),
        context_ref="test",
        requirements=[
            {
                "requirement_key": "TAX_INVOICE",
                "document_type_key": "tax_invoice",
                "requirement_level": "REQUIRED",
                "requirement_status": "PENDING",
                "display_label": "Tax Invoice",
                "condition_key": None,
            }
        ],
        audit_documents=[
            {
                "di_document_id": document_id,
                "client_upload_id": "client-1",
                "requirement_key": "TAX_INVOICE",
                "classified_document_type_key": "tax_invoice",
                "capture_status": "CLASSIFIED",
                "original_filename": "invoice.pdf",
                "content_type": "application/pdf",
            }
        ],
        di_documents=[
            {
                "documentId": str(document_id),
                "clientUploadId": "client-1",
                "state": "CLASSIFIED",
                "classifiedDocumentTypeKey": "tax_invoice",
                "originalFilename": "invoice.pdf",
                "contentUrl": None,
                "processingStatus": "PROCESSING",
            }
        ],
        submitted=False,
    )

    assert response.canSubmit is True
    assert response.requirements[0].state == "UPLOADED"
    assert response.requirements[0].blocksContinue is False
