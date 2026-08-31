from __future__ import annotations

from collections.abc import Callable
from typing import Any

from audit_core import uc03_document_capture_v2 as capture_v2

_GST_CONDITION = "gstApplicable"
_CORPORATE_CONDITION = "corporateCustomer"
_EXCLUSIVE_CONDITIONS = {_GST_CONDITION, _CORPORATE_CONDITION}
_IDENTITY_MARKERS = ("PAN", "AADHAAR", "AADHAR")


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
    """Apply the GST/Corporate business rule without blocking Booking progression.

    GST and Corporate are mutually exclusive. Evidence continues to remain visible when
    contradictory documents are present so Audit can raise an exception; the capture
    flow must never hide or discard evidence and must never block the Booking process.
    """

    gst_active = _condition_is_active(response, _GST_CONDITION)
    corporate_active = _condition_is_active(response, _CORPORATE_CONDITION)
    conflict = gst_active and corporate_active

    for requirement in response.requirements:
        condition_key = requirement.conditionKey
        if condition_key not in _EXCLUSIVE_CONDITIONS:
            continue

        if conflict:
            requirement.needsDecision = False
            requirement.blocksContinue = False
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

    return response


def _is_identity_requirement(requirement: capture_v2.CaptureV2Requirement) -> bool:
    searchable = f"{requirement.documentTypeKey} {requirement.label}".upper()
    return any(marker in searchable for marker in _IDENTITY_MARKERS)


def _apply_identity_document_choice(
    response: capture_v2.BookingCaptureV2Response,
) -> capture_v2.BookingCaptureV2Response:
    """Treat PAN and Aadhaar as one identity-document choice for audit presentation."""

    identity_requirements = [
        requirement
        for requirement in response.requirements
        if _is_identity_requirement(requirement)
    ]
    if len(identity_requirements) < 2:
        return response

    required_identity = [
        requirement
        for requirement in identity_requirements
        if requirement.requirementLevel.upper() == "REQUIRED"
    ]
    if not required_identity:
        return response

    identity_present = any(
        requirement.document is not None and requirement.state == "UPLOADED"
        for requirement in identity_requirements
    )
    if identity_present:
        for requirement in required_identity:
            requirement.blocksContinue = False
    return response


def _apply_non_blocking_audit_policy(
    response: capture_v2.BookingCaptureV2Response,
) -> capture_v2.BookingCaptureV2Response:
    """Audit observations never stop the Booking business process.

    Requirement states, missing evidence, unresolved optional applicability and
    contradictory evidence remain visible for audit follow-up. They are deliberately
    not converted into a continuation gate.
    """

    for requirement in response.requirements:
        requirement.blocksContinue = False
    response.canContinue = True
    return response


def _apply_capture_business_rules(
    response: capture_v2.BookingCaptureV2Response,
) -> capture_v2.BookingCaptureV2Response:
    response = _apply_gst_corporate_exclusivity(response)
    response = _apply_identity_document_choice(response)
    return _apply_non_blocking_audit_policy(response)


def install_uc03_v2_capture_business_rules() -> None:
    """Install additive V2-only capture/review rules without changing V1 behavior."""

    from audit_core.uc03_booking_commercial_components import (
        install_uc03_booking_commercial_components,
    )
    from audit_core.uc03_booking_review_decisions import (
        install_uc03_booking_review_decisions,
    )
    from audit_core.uc03_booking_rule_trigger import (
        install_uc03_booking_review_rule_trigger,
    )
    from audit_core.uc03_di_core_persistence import install_uc03_di_core_persistence

    # Register the complete Booking Form extraction contract before Review routes
    # handle any request. This extends existing Core owners; it creates no new table.
    install_uc03_booking_commercial_components()
    install_uc03_booking_review_decisions()
    install_uc03_di_core_persistence()
    install_uc03_booking_review_rule_trigger()
    if getattr(capture_v2, "_gst_corporate_exclusivity_installed", False):
        return

    original: Callable[..., capture_v2.BookingCaptureV2Response] = (
        capture_v2._build_capture_response
    )

    def wrapped(*args: Any, **kwargs: Any) -> capture_v2.BookingCaptureV2Response:
        return _apply_capture_business_rules(original(*args, **kwargs))

    capture_v2._build_capture_response = wrapped  # type: ignore[assignment]
    capture_v2._gst_corporate_exclusivity_installed = True
