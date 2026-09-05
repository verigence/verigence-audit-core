import pytest

from audit_core.errors import AuditCoreError
from audit_core.main import app
from audit_core.uc03_booking_details import BookingDetailsCommand
from audit_core.uc03_booking_v2 import (
    _mandatory_booking_documents_complete,
    _validate_declaration_alignment,
)


def _command(**overrides):
    values = {
        "priceListId": None,
        "customerType": "INDIVIDUAL",
        "dealType": "IN_SCOPE",
        "dealSource": "WALK_IN",
        "leadSource": "IN_HOUSE",
        "registrationState": "CH",
        "territoryCategorization": "SAME_TERRITORY",
        "districtName": "OTHER",
        "registrationType": "PERMANENT",
        "registrationCategory": "PRIVATE",
        "outrightPurchase": True,
        "tradeIn": False,
        "gstBenefit": False,
        "corporateIdAvailable": None,
    }
    values.update(overrides)
    return BookingDetailsCommand(**values)


def _requirement(
    key: str,
    *,
    document_type: str | None = None,
    level: str = "REQUIRED",
):
    return {
        "requirement_key": key,
        "document_type_key": document_type or key,
        "requirement_level": level,
    }


def _classified_document(key: str):
    return {
        "requirement_key": key,
        "capture_status": "CLASSIFIED",
    }


def test_v2_booking_details_and_submit_routes_are_registered() -> None:
    paths = app.openapi()["paths"]
    base = "/v2/tenants/{tenant_id}/journeys/{journey_id}/booking"
    assert f"{base}/details" in paths
    assert f"{base}/submit" in paths
    assert "get" in paths[f"{base}/details"]
    assert "post" in paths[f"{base}/submit"]


def test_v2_submit_rejects_trade_in_mismatch_with_documents() -> None:
    with pytest.raises(AuditCoreError) as error:
        _validate_declaration_alignment(
            _command(tradeIn=True),
            {
                "exchangeTaken": {
                    "applicable": False,
                    "document_available": None,
                }
            },
        )
    assert "Trade-In" in error.value.detail


def test_v2_submit_rejects_corporate_mismatch_with_documents() -> None:
    with pytest.raises(AuditCoreError) as error:
        _validate_declaration_alignment(
            _command(),
            {
                "corporateCustomer": {
                    "applicable": True,
                    "document_available": False,
                }
            },
        )
    assert "Corporate customer" in error.value.detail


def test_v2_submit_accepts_matching_document_declarations() -> None:
    command = _command(
        customerType="CORPORATE",
        tradeIn=True,
        gstBenefit=True,
        corporateIdAvailable=False,
    )
    _validate_declaration_alignment(
        command,
        {
            "exchangeTaken": {"applicable": True, "document_available": False},
            "gstApplicable": {"applicable": True, "document_available": False},
            "corporateCustomer": {"applicable": True, "document_available": False},
        },
    )


def test_booking_completion_requires_all_non_identity_mandatory_documents() -> None:
    requirements = [
        _requirement("booking_docket"),
        _requirement("pan_card"),
        _requirement("aadhaar"),
        _requirement("minimum_booking_payment_proof"),
    ]
    documents = [
        _classified_document("booking_docket"),
        _classified_document("pan_card"),
    ]

    assert _mandatory_booking_documents_complete(requirements, documents) is False


def test_booking_completion_accepts_existing_pan_or_aadhaar_identity_choice() -> None:
    requirements = [
        _requirement("booking_docket"),
        _requirement("pan_card"),
        _requirement("aadhaar"),
        _requirement("minimum_booking_payment_proof"),
        _requirement("gst_certificate", level="CONDITIONAL"),
    ]
    documents = [
        _classified_document("booking_docket"),
        _classified_document("aadhaar"),
        _classified_document("minimum_booking_payment_proof"),
    ]

    assert _mandatory_booking_documents_complete(requirements, documents) is True


def test_booking_completion_requires_a_classified_identity_document() -> None:
    requirements = [
        _requirement("booking_docket"),
        _requirement("pan_card"),
        _requirement("aadhaar"),
        _requirement("minimum_booking_payment_proof"),
    ]
    documents = [
        _classified_document("booking_docket"),
        _classified_document("minimum_booking_payment_proof"),
        {"requirement_key": "pan_card", "capture_status": "CLASSIFYING"},
    ]

    assert _mandatory_booking_documents_complete(requirements, documents) is False
