from decimal import Decimal
from uuid import UUID

from audit_core.uc03_sku_candidates import (
    _commercial_similarity,
    _label_similarity,
    rank_sku_candidates,
)


def _row(
    sku_id: str,
    sku_code: str,
    model: str,
    variant: str,
    amount: str,
    colour: str | None = None,
):
    return {
        "product_sku_id": UUID(sku_id),
        "sku_code": sku_code,
        "model_name": model,
        "variant_name": variant,
        "colour_name": colour,
        "master_total_amount": Decimal(amount),
        "same_date_version_count": 1,
    }


def test_label_similarity_normalizes_spacing_and_punctuation() -> None:
    assert _label_similarity("XUV 700", "XUV700") == Decimal("1")
    assert _label_similarity("AX7-L", "AX7 L") == Decimal("1")


def test_commercial_similarity_rewards_price_proximity() -> None:
    near, _, near_pct = _commercial_similarity(Decimal("2500000"), Decimal("2510000"))
    far, _, far_pct = _commercial_similarity(Decimal("2500000"), Decimal("3000000"))
    assert near > far
    assert near_pct < far_pct


def test_ranked_candidate_uses_model_variant_and_commercial_total() -> None:
    rows = [
        _row(
            "00000000-0000-0000-0000-000000000001",
            "XUV700-AX7L-R",
            "XUV700",
            "AX7 L",
            "2510000",
            "Red Rage",
        ),
        _row(
            "00000000-0000-0000-0000-000000000002",
            "XUV700-AX5-R",
            "XUV700",
            "AX5",
            "2300000",
            "Red Rage",
        ),
        _row(
            "00000000-0000-0000-0000-000000000003",
            "SCORPIO-N-Z8L",
            "Scorpio N",
            "Z8 L",
            "2500000",
            "Black",
        ),
    ]

    candidates = rank_sku_candidates(
        rows,
        model_name="XUV 700",
        variant_name="AX7L",
        total_commercial_amount=Decimal("2500000"),
        max_candidates=5,
    )

    assert candidates
    assert candidates[0].skuCode == "XUV700-AX7L-R"
    assert candidates[0].candidateStatus == "TENTATIVE"
    assert candidates[0].confirmationRequired is True
    assert candidates[0].modelScore == Decimal("1.0000")
    assert candidates[0].variantScore == Decimal("1.0000")
    assert candidates[0].commercialDifferenceAmount == Decimal("10000.00")


def test_exact_model_variant_is_not_auto_confirmed_even_at_exact_price() -> None:
    rows = [
        _row(
            "00000000-0000-0000-0000-000000000010",
            "MODEL-VARIANT",
            "Model X",
            "Variant Pro",
            "1000000",
        )
    ]
    candidate = rank_sku_candidates(
        rows,
        model_name="Model X",
        variant_name="Variant Pro",
        total_commercial_amount=Decimal("1000000"),
        max_candidates=1,
    )[0]
    assert candidate.score == Decimal("1.0000")
    assert candidate.candidateStatus == "TENTATIVE"
    assert candidate.confirmationRequired is True


def test_unrelated_low_score_rows_are_not_returned_as_false_candidates() -> None:
    rows = [
        _row(
            "00000000-0000-0000-0000-000000000020",
            "UNRELATED",
            "Completely Different",
            "Base",
            "500000",
        )
    ]
    candidates = rank_sku_candidates(
        rows,
        model_name="XUV700",
        variant_name="AX7 L",
        total_commercial_amount=Decimal("2500000"),
        max_candidates=5,
    )
    assert candidates == []
