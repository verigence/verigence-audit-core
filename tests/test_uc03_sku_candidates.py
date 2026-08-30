from decimal import Decimal
from uuid import UUID

from audit_core.uc03_sku_candidates import (
    SkuCandidate,
    _commercial_similarity,
    _label_similarity,
    _persist_most_likely_sku,
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


def _candidate() -> SkuCandidate:
    return SkuCandidate(
        rank=1,
        productSkuId=UUID("00000000-0000-0000-0000-000000000001"),
        skuCode="XUV700-AX7L-R",
        modelName="XUV700",
        variantName="AX7 L",
        colourName="Red Rage",
        displayLabel="XUV700 AX7 L *",
        masterTotalAmount=Decimal("2510000"),
        observedTotalCommercialAmount=Decimal("2500000"),
        commercialDifferenceAmount=Decimal("10000"),
        commercialDifferencePercent=Decimal("0.40"),
        score=Decimal("0.9967"),
        modelScore=Decimal("1"),
        variantScore=Decimal("1"),
        commercialScore=Decimal("0.9867"),
    )


class _ExistingResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _WriteResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _FakeConnection:
    def __init__(self, existing_status=None, write_rowcount: int = 1):
        self.existing_status = existing_status
        self.write_rowcount = write_rowcount
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), params))
        if len(self.calls) == 1:
            return _ExistingResult(self.existing_status)
        return _WriteResult(self.write_rowcount)


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
    assert candidates[0].displayLabel == "XUV700 AX7 L *"
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
    assert candidate.displayLabel.endswith(" *")


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


def test_top_candidate_is_written_as_tentative_when_not_confirmed() -> None:
    connection = _FakeConnection(existing_status=None)
    updated, confirmed_preserved = _persist_most_likely_sku(
        connection,
        tenant_id="tenant-1",
        journey_id=UUID("00000000-0000-0000-0000-000000000099"),
        candidate=_candidate(),
    )
    assert updated is True
    assert confirmed_preserved is False
    assert len(connection.calls) == 2
    sql, params = connection.calls[1]
    assert "selection_status" in sql
    assert "'TENTATIVE'" in sql
    assert params["selection_method"] == "BOOKING_COMMERCIAL_MATCH_V1"


def test_confirmed_sku_is_never_overwritten_by_inference() -> None:
    connection = _FakeConnection(existing_status="CONFIRMED")
    updated, confirmed_preserved = _persist_most_likely_sku(
        connection,
        tenant_id="tenant-1",
        journey_id=UUID("00000000-0000-0000-0000-000000000099"),
        candidate=_candidate(),
    )
    assert updated is False
    assert confirmed_preserved is True
    assert len(connection.calls) == 1
