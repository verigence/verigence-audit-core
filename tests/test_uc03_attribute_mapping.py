from audit_core.uc03_attribute_mapping import (
    AttributeCandidate,
    comparison_state,
    resolve_candidate,
    spec_for_field,
)


def _candidate(
    *,
    field_key: str,
    value: object,
    source: str,
    confidence: float,
    document_id: str,
) -> AttributeCandidate:
    return AttributeCandidate(
        field_key=field_key,
        value=value,
        confidence_score=confidence,
        document_id=document_id,
        document_type_key=source,
        document_label=source,
        original_filename=f"{source}.pdf",
        content_url=None,
        page_no=1,
        evidence_region=None,
        canonical_field_id="canonical-1",
        source_fact_version=1,
    )


def test_customer_name_source_priority_beats_higher_confidence() -> None:
    spec = spec_for_field("pan_name")
    assert spec is not None
    selected = resolve_candidate(
        spec,
        [
            _candidate(
                field_key="customer_name",
                value="Rahul K Sharma",
                source="booking_form",
                confidence=99.0,
                document_id="b",
            ),
            _candidate(
                field_key="pan_name",
                value="Rahul Sharma",
                source="pan_card",
                confidence=94.0,
                document_id="a",
            ),
        ],
    )
    assert selected is not None
    assert selected.document_type_key == "pan_card"
    assert selected.value == "Rahul Sharma"


def test_confidence_breaks_tie_within_same_source_priority() -> None:
    spec = spec_for_field("vehicle_model")
    assert spec is not None
    selected = resolve_candidate(
        spec,
        [
            _candidate(
                field_key="vehicle_model",
                value="XUV700",
                source="booking_form",
                confidence=91.0,
                document_id="a",
            ),
            _candidate(
                field_key="vehicle_model",
                value="XUV 700",
                source="booking_form",
                confidence=97.0,
                document_id="b",
            ),
        ],
    )
    assert selected is not None
    assert selected.document_id == "b"


def test_unknown_field_is_not_guessed_into_attribute() -> None:
    assert spec_for_field("customer_full_legal_display_name_v99") is None


def test_comparison_state_reports_match_mismatch_and_single_source() -> None:
    same = [
        _candidate(field_key="pan_name", value="Rahul Sharma", source="pan_card", confidence=96, document_id="a"),
        _candidate(field_key="aadhaar_name", value="  RAHUL   SHARMA ", source="aadhaar", confidence=95, document_id="b"),
    ]
    mismatch = [
        _candidate(field_key="pan_name", value="Rahul Sharma", source="pan_card", confidence=96, document_id="a"),
        _candidate(field_key="aadhaar_name", value="Rahul K Sharma", source="aadhaar", confidence=95, document_id="b"),
    ]
    single = [same[0]]
    assert comparison_state(same) == "MATCH"
    assert comparison_state(mismatch) == "MISMATCH"
    assert comparison_state(single) == "SINGLE_SOURCE"
