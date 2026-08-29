import pytest

from audit_core.errors import AuditCoreError
from audit_core.main import app
from audit_core.uc03_booking_details import BookingDetailsCommand
from audit_core.uc03_booking_v2 import _validate_declaration_alignment


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
