from audit_core.uc03_booking_rule_trigger import _booking_requirement_rule_specs


def _row(
    key: str,
    *,
    level: str = "REQUIRED",
    status: str = "SATISFIED",
    answer: str = "YES",
) -> dict[str, str]:
    return {
        "requirement_key": key,
        "requirement_level": level,
        "requirement_status": status,
        "answer": answer,
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
