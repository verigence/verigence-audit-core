from audit_core.uc03_document_review_v2 import _field_review_state


def test_review_field_at_92_is_ready() -> None:
    assert _field_review_state(value="ABC", confidence_score=92.0) == "READY"


def test_review_field_above_92_is_ready() -> None:
    assert _field_review_state(value="ABC", confidence_score=98.5) == "READY"


def test_review_field_below_92_needs_review() -> None:
    assert _field_review_state(value="ABC", confidence_score=91.99) == "NEEDS_REVIEW"


def test_review_field_without_confidence_needs_review() -> None:
    assert _field_review_state(value="ABC", confidence_score=None) == "NEEDS_REVIEW"


def test_review_field_without_value_needs_review() -> None:
    assert _field_review_state(value=None, confidence_score=99.0) == "NEEDS_REVIEW"
