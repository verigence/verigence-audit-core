from audit_core.uc03_booking_part1 import _normalized


def test_part1_master_normalization_is_exact_but_format_tolerant() -> None:
    assert _normalized("Scorpio N") == "scorpion"
    assert _normalized("  Z8-L  ") == "z8l"
    assert _normalized(None) is None
