from audit_core.uc03_customer_review import _normalize_mobile, _normalize_relation_type


def test_review_mobile_keeps_full_number_as_digits():
    assert _normalize_mobile("+91 98765-43210") == "919876543210"


def test_review_relation_type_normalizes_common_forms():
    assert _normalize_relation_type("s/o") == "S/O"
    assert _normalize_relation_type("Daughter Of") == "D/O"
    assert _normalize_relation_type("wife of") == "W/O"
