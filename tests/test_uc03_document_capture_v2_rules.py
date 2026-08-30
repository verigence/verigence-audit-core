from uuid import uuid4

from audit_core.uc03_document_capture_v2 import (
    BookingCaptureV2Response,
    CaptureV2Declaration,
    CaptureV2Document,
    CaptureV2Requirement,
)
from audit_core.uc03_document_capture_v2_rules import _apply_gst_corporate_exclusivity


def _document(document_type_key: str) -> CaptureV2Document:
    return CaptureV2Document(
        documentId=uuid4(),
        clientUploadId=str(uuid4()),
        state="CLASSIFIED",
        classifiedDocumentTypeKey=document_type_key,
        originalFilename=f"{document_type_key}.pdf",
    )


def _requirement(condition_key: str, document_type_key: str, document=None) -> CaptureV2Requirement:
    return CaptureV2Requirement(
        requirementKey=f"REQ_{condition_key}",
        label=condition_key,
        documentTypeKey=document_type_key,
        requirementLevel="CONDITIONAL",
        conditionKey=condition_key,
        applicabilityState="APPLICABLE" if document else "UNRESOLVED",
        state="UPLOADED" if document else "NEEDS_DECISION",
        document=document,
        needsDecision=document is None,
        blocksContinue=document is None,
    )


def _capture(gst_document=None, corporate_document=None) -> BookingCaptureV2Response:
    declarations = []
    if gst_document:
        declarations.append(
            CaptureV2Declaration(
                conditionKey="gstApplicable",
                applicable=True,
                documentAvailable=True,
                source="DOCUMENT",
            )
        )
    if corporate_document:
        declarations.append(
            CaptureV2Declaration(
                conditionKey="corporateCustomer",
                applicable=True,
                documentAvailable=True,
                source="DOCUMENT",
            )
        )
    return BookingCaptureV2Response(
        journeyId=uuid4(),
        externalContextRef="test",
        requirements=[
            _requirement("gstApplicable", "GST_CERTIFICATE", gst_document),
            _requirement("corporateCustomer", "CORPORATE_ID", corporate_document),
        ],
        uploads=[doc for doc in (gst_document, corporate_document) if doc is not None],
        declarations=declarations,
        canContinue=False,
    )


def test_gst_evidence_suppresses_corporate_question() -> None:
    capture = _apply_gst_corporate_exclusivity(_capture(gst_document=_document("GST_CERTIFICATE")))
    corporate = next(r for r in capture.requirements if r.conditionKey == "corporateCustomer")
    assert corporate.applicabilityState == "NOT_APPLICABLE"
    assert corporate.state == "NOT_APPLICABLE"
    assert corporate.needsDecision is False
    assert corporate.blocksContinue is False
    assert capture.canContinue is True


def test_corporate_evidence_suppresses_gst_question() -> None:
    capture = _apply_gst_corporate_exclusivity(_capture(corporate_document=_document("CORPORATE_ID")))
    gst = next(r for r in capture.requirements if r.conditionKey == "gstApplicable")
    assert gst.applicabilityState == "NOT_APPLICABLE"
    assert gst.state == "NOT_APPLICABLE"
    assert gst.needsDecision is False
    assert gst.blocksContinue is False
    assert capture.canContinue is True


def test_gst_and_corporate_evidence_together_block_capture() -> None:
    capture = _apply_gst_corporate_exclusivity(
        _capture(
            gst_document=_document("GST_CERTIFICATE"),
            corporate_document=_document("CORPORATE_ID"),
        )
    )
    exclusive = [r for r in capture.requirements if r.conditionKey in {"gstApplicable", "corporateCustomer"}]
    assert all(r.blocksContinue for r in exclusive)
    assert capture.canContinue is False
