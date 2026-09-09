from audit_core.uc03_booking_rule_trigger import (
    _booking_requirement_rule_specs,
    _requirement_satisfied,
)


def _row(
    key: str,
    *,
    level: str = "REQUIRED",
    status: str = "SATISFIED",
    answer: str = "YES",
    has_evidence: bool = False,
) -> dict[str, str | bool]:
    return {
        "requirement_key": key,
        "requirement_level": level,
        "requirement_status": status,
        "answer": answer,
        "has_evidence": has_evidence,
    }


def test_identity_choice_suppresses_pan_flag_when_aadhaar_is_satisfied() -> None:
    specs = _booking_requirement_rule_specs(
        [
            _row("pan_card", status="PENDING", answer="UNANSWERED"),
            _row("aadhaar"),
        ]
    )

    assert "BK_PAN_PRESENT" not in {spec.rule_key for spec in specs}


def test_booking_checkpoint_maps_specific_missing_requirements_to_rules() -> None:
    specs = _booking_requirement_rule_specs(
        [
            _row("booking_docket", status="PENDING", answer="NO"),
            _row("pan_card", status="PENDING", answer="NO"),
            _row("aadhaar", status="PENDING", answer="NO"),
            _row("minimum_booking_payment_proof", status="PENDING", answer="NO"),
        ]
    )

    assert {spec.rule_key for spec in specs} == {
        "BK_DOCKET_PRESENT",
        "BK_PAN_PRESENT",
        "BK_MIN_BOOKING_PROOF_PRESENT",
    }


def test_conditional_and_other_required_requirements_get_checkpoint_rules() -> None:
    specs = _booking_requirement_rule_specs(
        [
            _row("gst_certificate", level="CONDITIONAL", status="PENDING", answer="NO"),
            _row("some_future_required_doc", status="PENDING", answer="UNANSWERED"),
        ]
    )

    by_rule = {spec.rule_key: spec for spec in specs}
    assert by_rule["BK_CONDITIONAL_DOCS_ADDRESSED"].requirement_keys == ("gst_certificate",)
    assert by_rule["BK_REQUIRED_CAPTURE_COMPLETE"].requirement_keys == (
        "some_future_required_doc",
    )


def test_active_evidence_satisfies_a_requirement_even_with_no_explicit_answer() -> None:
    """Regression: journey_document_requirements.requirement_status is never
    actually transitioned to SATISFIED for Booking's V2 "upload everything"
    flow (only Delivery's own explicit per-document answer endpoint does
    that), and nothing in that flow ever answers a per-document applicability
    question either -- so requirement_status stayed PENDING and answer stayed
    UNANSWERED forever, regardless of how thoroughly a PC reviewed the actual
    linked evidence. Every requirement with real evidence linked used to be
    flagged outstanding unconditionally; it must not be once evidence exists.
    """
    assert _requirement_satisfied(
        _row("booking_docket", status="PENDING", answer="UNANSWERED", has_evidence=True)
    )

    specs = _booking_requirement_rule_specs(
        [
            _row("booking_docket", status="PENDING", answer="UNANSWERED", has_evidence=True),
            _row("pan_card", status="PENDING", answer="UNANSWERED", has_evidence=True),
            _row("minimum_booking_payment_proof", status="PENDING", answer="UNANSWERED", has_evidence=True),
        ]
    )
    assert specs == []
