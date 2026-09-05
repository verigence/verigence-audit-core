from __future__ import annotations

from collections import deque
from uuid import uuid4

from audit_core.main import app
from audit_core.uc03_journey_overview_projection import (
    _documents,
    _masked_phone,
    _receipts,
    _reviewed_booking_projection,
    _reviewed_legal_name,
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _ScriptedConnection:
    def __init__(self, *results):
        self._results = deque(results)

    def execute(self, *_args, **_kwargs):
        return _Rows(self._results.popleft())


def test_projection_route_precedes_legacy_overview_route() -> None:
    matches = [
        route
        for route in app.routes
        if getattr(route, "path", None)
        == "/v1/tenants/{tenant_id}/uc03/journeys/{journey_id}/overview"
    ]
    assert len(matches) == 2
    assert matches[0].endpoint.__name__ == "get_journey_overview_projection"


def test_reviewed_booking_projection_only_uses_unambiguous_values() -> None:
    rows = [
        {
            "booking_reference_number": "BK-42",
            "customer_email": "buyer@example.com",
            "vehicle_model": "SCORPIO N",
        },
        {
            "booking_reference_number": "BK-42",
            "customer_email": "buyer@example.com",
            "vehicle_model": "THAR",
        },
    ]
    projection = _reviewed_booking_projection(rows)
    assert projection["booking_reference_number"] == "BK-42"
    assert projection["customer_email"] == "buyer@example.com"
    assert "vehicle_model" not in projection


def test_reviewed_legal_name_does_not_guess_across_conflicting_identity_documents() -> None:
    assert _reviewed_legal_name(
        [
            {"pan_name": "Test Customer", "aadhaar_name": None},
            {"pan_name": None, "aadhaar_name": " test   customer "},
        ]
    ) == "Test Customer"
    assert _reviewed_legal_name(
        [
            {"pan_name": "Test Customer", "aadhaar_name": None},
            {"pan_name": None, "aadhaar_name": "Different Customer"},
        ]
    ) is None


def test_masked_phone_never_reconstructs_full_contact() -> None:
    assert _masked_phone("+91 98765 43210") == "******3210"
    assert _masked_phone("3210") == "******3210"
    assert _masked_phone("12") is None


def test_documents_include_v2_capture_and_dedupe_legacy_evidence() -> None:
    document_id = uuid4()
    legacy_only_document_id = uuid4()
    connection = _ScriptedConnection(
        [
            {
                "documentId": document_id,
                "requirementKey": "minimum_booking_payment_proof",
                "documentTypeKey": "dealer_receipt",
                "processArea": "BOOKING",
                "processingStatus": "CLASSIFIED",
                "originalFilename": "receipt-1.pdf",
                "linkedAtUtc": None,
            }
        ],
        [
            {
                "evidenceId": uuid4(),
                "documentId": document_id,
                "documentTypeKey": "dealer_receipt",
                "evidencePurpose": "BOOKING",
                "processArea": "BOOKING",
                "processingStatus": "COMPLETED",
                "verificationStatus": "VERIFIED",
                "confirmationStatus": "CONFIRMED",
                "linkedAtUtc": None,
            },
            {
                "evidenceId": uuid4(),
                "documentId": legacy_only_document_id,
                "documentTypeKey": "pan_card",
                "evidencePurpose": "BOOKING",
                "processArea": "BOOKING",
                "processingStatus": "COMPLETED",
                "verificationStatus": "VERIFIED",
                "confirmationStatus": "CONFIRMED",
                "linkedAtUtc": None,
            },
        ],
    )

    documents = _documents(
        connection,
        tenant_id="tenant-1",
        journey_id=uuid4(),
        review_statuses={"BOOKING": "PENDING"},
    )
    assert len(documents) == 2
    assert str(documents[0]["documentId"]) == str(document_id)
    assert documents[0]["originalFilename"] == "receipt-1.pdf"
    assert documents[0]["reviewStatus"] == "PENDING"
    assert str(documents[1]["documentId"]) == str(legacy_only_document_id)


def test_receipts_keep_every_reviewed_receipt_and_pending_capture_distinct() -> None:
    reviewed_id = uuid4()
    pending_id = uuid4()
    connection = _ScriptedConnection(
        [
            {
                "documentId": reviewed_id,
                "evidenceId": None,
                "dealerName": "Dealer",
                "dealerGstin": None,
                "customerName": "Customer",
                "customerPhone": "9876543210",
                "receiptNumber": "R-1",
                "receiptDate": None,
                "amount": 50000,
                "paymentMethodCode": "UPI",
                "paymentReference": "UTR-1",
                "paymentReferenceDate": None,
                "bankName": None,
                "bankLocation": None,
                "bookingReference": "BK-1",
                "remarks": None,
                "amountInWords": None,
                "originalFilename": "receipt-1.pdf",
                "stageCode": "BOOKING",
                "captureStatus": "CLASSIFIED",
            }
        ],
        [
            {
                "documentId": reviewed_id,
                "originalFilename": "receipt-1.pdf",
                "stageCode": "BOOKING",
                "captureStatus": "CLASSIFIED",
                "documentTypeKey": "dealer_receipt",
            },
            {
                "documentId": pending_id,
                "originalFilename": "receipt-2.pdf",
                "stageCode": "BOOKING",
                "captureStatus": "CLASSIFIED",
                "documentTypeKey": "dealer_receipt",
            },
        ],
    )

    receipts = _receipts(
        connection,
        tenant_id="tenant-1",
        journey_id=uuid4(),
        review_statuses={"BOOKING": "PENDING"},
    )
    assert len(receipts) == 2
    assert receipts[0]["receiptNumber"] == "R-1"
    assert receipts[0]["customerPhone"] == "******3210"
    assert receipts[0]["reviewStatus"] == "VERIFIED"
    assert str(receipts[1]["documentId"]) == str(pending_id)
    assert receipts[1]["reviewStatus"] == "PENDING"
