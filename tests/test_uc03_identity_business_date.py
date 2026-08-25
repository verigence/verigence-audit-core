from audit_core import uc03_booking_capture
from audit_core.uc03_identity_business_date import install_uc03_identity_business_date


def test_identity_publication_boundary_preserves_entered_name_contract() -> None:
    install_uc03_identity_business_date()

    assert "customer_name" not in uc03_booking_capture._SUPPORTED_PROPOSAL_FIELDS["booking_form"]
    assert "customer_name" not in uc03_booking_capture._SUPPORTED_PROPOSAL_FIELDS["booking_docket"]
    assert uc03_booking_capture._SUPPORTED_PROPOSAL_FIELDS["aadhaar"] == {"aadhaar_name"}
    assert uc03_booking_capture._PROPOSAL_CAPTURE_MAP["pan_name"] == "CUSTOMER_NAME"
    assert uc03_booking_capture._PROPOSAL_CAPTURE_MAP["aadhaar_name"] == "CUSTOMER_NAME"


def test_booking_date_remains_typed_actual_booking_date_capture() -> None:
    install_uc03_identity_business_date()

    assert "booking_date" in uc03_booking_capture._SUPPORTED_PROPOSAL_FIELDS["booking_form"]
    assert "booking_date" in uc03_booking_capture._SUPPORTED_PROPOSAL_FIELDS["booking_docket"]
    assert uc03_booking_capture._PROPOSAL_CAPTURE_MAP["booking_date"] == "BOOKING_DATE"
