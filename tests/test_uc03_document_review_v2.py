from audit_core.uc03_document_review_v2 import _field_review_state


def test_review_field_at_90_is_ready() -> None:
    assert _field_review_state(value="ABC", confidence_score=90.0) == "READY"


def test_review_field_above_90_is_ready() -> None:
    assert _field_review_state(value="ABC", confidence_score=98.5) == "READY"


def test_review_field_below_90_needs_review() -> None:
    assert _field_review_state(value="ABC", confidence_score=89.99) == "NEEDS_REVIEW"


def test_review_field_without_confidence_needs_review() -> None:
    assert _field_review_state(value="ABC", confidence_score=None) == "NEEDS_REVIEW"


def test_empty_high_confidence_field_is_not_a_review_exception() -> None:
    # UC03 review work is exception-only for populated DI facts below 90%.
    assert _field_review_state(value=None, confidence_score=99.0) == "READY"
