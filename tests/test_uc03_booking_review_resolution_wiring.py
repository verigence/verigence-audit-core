from audit_core import uc03_attribute_resolution as attribute_resolution
from audit_core import uc03_booking_review_decisions as booking_review_decisions


def test_booking_review_confirm_uses_attribute_resolution_owner() -> None:
    assert (
        booking_review_decisions.apply_supported_operational_attribute
        is attribute_resolution.apply_supported_operational_attribute
    )
    assert (
        booking_review_decisions.record_attribute_resolution
        is attribute_resolution.record_attribute_resolution
    )


def test_booking_review_does_not_call_missing_review_v2_resolution_exports() -> None:
    code = booking_review_decisions.confirm_booking_review_v2_with_decisions.__code__
    assert "apply_supported_operational_attribute" not in code.co_names or (
        booking_review_decisions.apply_supported_operational_attribute
        is attribute_resolution.apply_supported_operational_attribute
    )
    assert "record_attribute_resolution" not in code.co_names or (
        booking_review_decisions.record_attribute_resolution
        is attribute_resolution.record_attribute_resolution
    )
