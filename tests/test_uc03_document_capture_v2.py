from uuid import UUID

from audit_core.uc03_document_capture_v2 import _build_capture_response


JOURNEY_ID = UUID("11111111-1111-1111-1111-111111111111")
DOCUMENT_ID = UUID("22222222-2222-2222-2222-222222222222")


def _requirement(
    *,
    key: str = "booking_docket",
    document_type: str = "booking_docket",
    level: str = "REQUIRED",
    condition: str | None = None,
) -> dict[str, object]:
    return {
        "requirement_key": key,
        "display_label": key.replace("_", " ").title(),
        "document_type_key": document_type,
        "requirement_level": level,
        "condition_key": condition,
    }


def _classified_document(
    *,
    document_type: str = "booking_docket",
) -> dict[str, object]:
    return {
        "documentId": str(DOCUMENT_ID),
        "clientUploadId": "client-upload-1",
        "state": "CLASSIFIED",
        "classifiedDocumentTypeKey": document_type,
        "originalFilename": "booking.pdf",
        "contentUrl": "https://example.test/signed-document",
        "processingStatus": "PROCESSING",
    }


def _response(
    *,
    requirements: list[dict[str, object]],
    declarations: dict[str, dict[str, object]] | None = None,
    audit_documents: list[dict[str, object]] | None = None,
    di_documents: list[dict[str, object]] | None = None,
):
    return _build_capture_response(
        journey_id=JOURNEY_ID,
        context_ref="journey:11111111-1111-1111-1111-111111111111",
        requirements=requirements,
        declaration_rows=declarations or {},
        audit_documents=audit_documents or [],
        di_documents=di_documents or [],
    )


def test_required_document_missing_blocks_screen_two() -> None:
    result = _response(requirements=[_requirement()])

    assert result.canContinue is False
    assert result.requirements[0].state == "NOT_UPLOADED"
    assert result.requirements[0].blocksContinue is True


def test_required_classified_document_allows_screen_two() -> None:
    result = _response(
        requirements=[_requirement()],
        audit_documents=[
            {
                "di_document_id": DOCUMENT_ID,
                "requirement_key": "booking_docket",
            }
        ],
        di_documents=[_classified_document()],
    )

    assert result.canContinue is True
    assert result.requirements[0].state == "UPLOADED"
    assert result.requirements[0].canView is True
    assert result.requirements[0].canDelete is True


def test_unknown_upload_does_not_satisfy_required_requirement() -> None:
    unknown = _classified_document()
    unknown["state"] = "UNKNOWN"
    unknown["classifiedDocumentTypeKey"] = None

    result = _response(
        requirements=[_requirement()],
        audit_documents=[
            {
                "di_document_id": DOCUMENT_ID,
                "requirement_key": None,
            }
        ],
        di_documents=[unknown],
    )

    assert result.canContinue is False
    assert result.requirements[0].state == "NOT_UPLOADED"
    assert result.requirements[0].blocksContinue is True
    assert result.uploads[0].state == "UNKNOWN"


def test_unresolved_conditional_requirement_blocks_screen_two() -> None:
    result = _response(
        requirements=[
            _requirement(
                key="gst_certificate",
                document_type="gst_certificate",
                level="CONDITIONAL",
                condition="gstApplicable",
            )
        ]
    )

    assert result.canContinue is False
    assert result.requirements[0].applicabilityState == "UNRESOLVED"
    assert result.requirements[0].state == "NEEDS_DECISION"
    assert result.requirements[0].needsDecision is True


def test_applicable_available_conditional_document_missing_blocks_screen_two() -> None:
    result = _response(
        requirements=[
            _requirement(
                key="gst_certificate",
                document_type="gst_certificate",
                level="CONDITIONAL",
                condition="gstApplicable",
            )
        ],
        declarations={
            "gstApplicable": {
                "applicable": True,
                "document_available": True,
            }
        },
    )

    assert result.canContinue is False
    assert result.requirements[0].state == "NOT_UPLOADED"
    assert result.requirements[0].blocksContinue is True


def test_applicable_but_document_unavailable_is_recorded_and_does_not_block() -> None:
    result = _response(
        requirements=[
            _requirement(
                key="corporate_id",
                document_type="corporate_id",
                level="CONDITIONAL",
                condition="corporateCustomer",
            )
        ],
        declarations={
            "corporateCustomer": {
                "applicable": True,
                "document_available": False,
            }
        },
    )

    assert result.canContinue is True
    assert result.requirements[0].applicabilityState == "APPLICABLE"
    assert result.requirements[0].state == "ACKNOWLEDGED_MISSING"
    assert result.requirements[0].blocksContinue is False
    assert result.declarations[0].applicable is True
    assert result.declarations[0].documentAvailable is False
    assert result.declarations[0].source == "PC"


def test_not_applicable_conditional_requirement_does_not_block() -> None:
    result = _response(
        requirements=[
            _requirement(
                key="trade_in_vehicle_rc",
                document_type="vehicle_rc",
                level="CONDITIONAL",
                condition="exchangeTaken",
            )
        ],
        declarations={
            "exchangeTaken": {
                "applicable": False,
                "document_available": None,
            }
        },
    )

    assert result.canContinue is True
    assert result.requirements[0].applicabilityState == "NOT_APPLICABLE"
    assert result.requirements[0].state == "NOT_APPLICABLE"
    assert result.requirements[0].blocksContinue is False
