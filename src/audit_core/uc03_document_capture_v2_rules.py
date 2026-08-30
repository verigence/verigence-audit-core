from __future__ import annotations

from collections.abc import Callable
from typing import Any

from audit_core import uc03_document_capture_v2 as capture_v2

_GST_CONDITION = "gstApplicable"
_CORPORATE_CONDITION = "corporateCustomer"
_EXCLUSIVE_CONDITIONS = {_GST_CONDITION, _CORPORATE_CONDITION}


def _condition_is_active(response: capture_v2.BookingCaptureV2Response, condition_key: str) -> bool:
    declaration = next(
        (item for item in response.declarations if item.conditionKey == condition_key),
        None,
    )
    if declaration is not None and declaration.applicable:
        return True

    return any(
        requirement.conditionKey == condition_key
        and requirement.document is not None
        and requirement.state == "UPLOADED"
        for requirement in response.requirements
    )


def _apply_gst_corporate_exclusivity(
    response: capture_v2.BookingCaptureV2Response,
) -> capture_v2.BookingCaptureV2Response:
    """Apply the Booking rule that GST and Corporate cannot both be applicable.

    Evidence remains the source of truth. If one side is positively established by a
    classified document or an explicit PC declaration, the other side becomes
    NOT_APPLICABLE without asking the PC another question. If both sides are positively
    established, the capture is blocked until the contradictory evidence/declaration is
    corrected; we never silently discard audit evidence.
    """

    gst_active = _condition_is_active(response, _GST_CONDITION)
    corporate_active = _condition_is_active(response, _CORPORATE_CONDITION)
    conflict = gst_active and corporate_active

    for requirement in response.requirements:
        condition_key = requirement.conditionKey
        if condition_key not in _EXCLUSIVE_CONDITIONS:
            continue

        if conflict:
            requirement.blocksContinue = True
            requirement.needsDecision = False
            continue

        suppress_condition = (
            gst_active and condition_key == _CORPORATE_CONDITION
        ) or (
            corporate_active and condition_key == _GST_CONDITION
        )
        if suppress_condition and requirement.document is None:
            requirement.applicabilityState = "NOT_APPLICABLE"
            requirement.state = "NOT_APPLICABLE"
            requirement.needsDecision = False
            requirement.blocksContinue = False

    response.canContinue = not any(
        requirement.blocksContinue for requirement in response.requirements
    )
    return response


def install_uc03_v2_capture_business_rules() -> None:
    """Install additive V2-only capture/review rules without changing V1 behavior."""

    from audit_core.uc03_booking_review_decisions import (
        install_uc03_booking_review_decisions,
    )

    install_uc03_booking_review_decisions()
    if getattr(capture_v2, "_gst_corporate_exclusivity_installed", False):
        return

    original: Callable[..., capture_v2.BookingCaptureV2Response] = (
        capture_v2._build_capture_response
    )

    def wrapped(*args: Any, **kwargs: Any) -> capture_v2.BookingCaptureV2Response:
        return _apply_gst_corporate_exclusivity(original(*args, **kwargs))

    capture_v2._build_capture_response = wrapped  # type: ignore[assignment]
    capture_v2._gst_corporate_exclusivity_installed = True
