import inspect
from uuid import uuid4

from audit_core.uc03_delivery_capture_v2 import (
    _build_delivery_capture_response,
    _resyncable_document_ids,
    create_delivery_upload_intents_v2,
    delete_delivery_document_v2,
    finalize_delivery_document_v2,
    resync_delivery_capture_v2,
)


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


def test_resyncable_document_ids_only_includes_classified_documents() -> None:
    # Regression: live Delivery journey with 15 documents where DI had
    # already classified 11 and extracted 6 of them, but audit-core's own
    # evidence cache (processing_status_cache etc.) was NULL for all 15 --
    # _sync_booking_document had never durably completed for this journey,
    # and DI will not retry a webhook it already got a fast 200 OK for.
    classified_id = uuid4()
    documents = [
        {"di_document_id": classified_id, "capture_status": "CLASSIFIED"},
        {"di_document_id": uuid4(), "capture_status": "UNKNOWN"},
        {"di_document_id": uuid4(), "capture_status": "RECEIVING"},
        {"di_document_id": uuid4(), "capture_status": "STORED"},
        {"di_document_id": uuid4(), "capture_status": "CLASSIFYING"},
        {"di_document_id": uuid4(), "capture_status": "FAILED"},
    ]

    assert _resyncable_document_ids(documents) == [classified_id]


def test_resyncable_document_ids_is_case_insensitive_and_handles_missing_status() -> None:
    lower_id = uuid4()
    documents = [
        {"di_document_id": lower_id, "capture_status": "classified"},
        {"di_document_id": uuid4(), "capture_status": None},
        {"di_document_id": uuid4()},
    ]

    assert _resyncable_document_ids(documents) == [lower_id]


def test_resync_endpoint_queues_one_background_task_per_resyncable_document() -> None:
    # Source-inspected: exercising the full route (auth, a real BackgroundTasks
    # dispatch, DI/Security client construction) needs infrastructure this
    # file's other tests don't set up. What matters for the regression this
    # endpoint exists to fix is that it (a) re-authorizes against the
    # journey, (b) refreshes classification status from DI's own live state
    # before filtering -- otherwise a Journey whose Delivery capture screen
    # has not been reopened since classification finished silently resyncs 0
    # documents against a stale local cache, (c) filters to resyncable
    # documents through the function above (not some inline duplicate of the
    # filter), and (d) queues the same _run_sync_booking_document_task the DI
    # webhook itself uses, so a manually-triggered resync goes through the
    # identical, already-tested pipeline rather than a parallel one.
    source = inspect.getsource(resync_delivery_capture_v2)
    assert "_authorize_delivery(" in source
    assert "_ensure_di_context(" in source
    assert "_reconcile_delivery_documents(" in source
    assert "_resyncable_document_ids(" in source
    assert "background_tasks.add_task(" in source
    assert "_run_sync_booking_document_task" in source


def test_uploading_and_finalizing_after_submission_is_allowed_but_delete_stays_locked() -> None:
    # Documents legitimately keep arriving after the PC has moved on to
    # Delivery Details (a late invoice, a corrected receipt) -- confirmed
    # live need. Only deleting already-submitted evidence should stay
    # locked. Source-inspected for the same reason as the resync endpoint
    # above: exercising the full routes needs infrastructure this file's
    # other tests don't set up; what matters is that the submission-complete
    # conflict check is gone from the two write paths that add evidence, and
    # still present on the one that removes it.
    upload_source = inspect.getsource(create_delivery_upload_intents_v2)
    finalize_source = inspect.getsource(finalize_delivery_document_v2)
    delete_source = inspect.getsource(delete_delivery_document_v2)

    assert "capture_completed_at_utc" not in upload_source
    assert "capture_completed_at_utc" not in finalize_source
    assert "capture_completed_at_utc" in delete_source
