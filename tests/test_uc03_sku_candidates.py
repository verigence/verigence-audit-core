from decimal import Decimal
from uuid import UUID

from audit_core.uc03_sku_candidates import (
    SkuCandidate,
    _commercial_similarity,
    _exact_direct_sku_matches,
    _label_similarity,
    _persist_sku_selection,
    _resolved_candidate,
    _unique_booking_matches,
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


def _candidate(*, tentative: bool = True) -> SkuCandidate:
    return SkuCandidate(
        rank=1,
        productSkuId=UUID("00000000-0000-0000-0000-000000000001"),
        skuCode="XUV700-AX7L-R",
        modelName="XUV700",
        variantName="AX7 L",
        colourName="Red Rage",
        displayLabel="XUV700 AX7 L *" if tentative else "XUV700 AX7 L",
        masterTotalAmount=Decimal(2510000),
        observedTotalCommercialAmount=Decimal(2500000),
        commercialDifferenceAmount=Decimal(10000),
        commercialDifferencePercent=Decimal("0.40"),
        score=Decimal("0.9967"),
        modelScore=Decimal(1),
        variantScore=Decimal(1),
        commercialScore=Decimal("0.9867"),
        candidateStatus="TENTATIVE" if tentative else "CONFIRMED",
        confirmationRequired=tentative,
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
    assert _label_similarity("XUV 700", "XUV700") == Decimal(1)
    assert _label_similarity("AX7-L", "AX7 L") == Decimal(1)


def test_commercial_similarity_rewards_price_proximity() -> None:
    near, _, near_pct = _commercial_similarity(Decimal(2500000), Decimal(2510000))
    far, _, far_pct = _commercial_similarity(Decimal(2500000), Decimal(3000000))
    assert near > far
    assert near_pct < far_pct


def test_explicit_booking_sku_code_maps_directly_to_master_row() -> None:
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
    ]
    matches = _exact_direct_sku_matches(rows, sku_code="xuv700 ax7l r")
    assert len(matches) == 1
    assert matches[0]["sku_code"] == "XUV700-AX7L-R"


def test_unique_model_price_booking_match_is_resolved_not_tentative() -> None:
    rows = [
        _row(
            "00000000-0000-0000-0000-000000000010",
            "MODEL-X-PRO",
            "Model X",
            "Variant Pro",
            "1000000",
            "Red",
        ),
        _row(
            "00000000-0000-0000-0000-000000000011",
            "MODEL-X-BASE",
            "Model X",
            "Variant Base",
            "850000",
            "Red",
        ),
    ]
    matches = _unique_booking_matches(
        rows,
        model_name="Model X",
        variant_name=None,
        colour_name=None,
        total_commercial_amount=Decimal(1000000),
    )
    assert len(matches) == 1

    candidate = _resolved_candidate(
        matches[0],
        model_name="Model X",
        variant_name=None,
        total_commercial_amount=Decimal(1000000),
    )
    assert candidate.skuCode == "MODEL-X-PRO"
    assert candidate.candidateStatus == "CONFIRMED"
    assert candidate.confirmationRequired is False
    assert not candidate.displayLabel.endswith(" *")


def test_multiple_model_price_matches_remain_tentative() -> None:
    rows = [
        _row(
            "00000000-0000-0000-0000-000000000020",
            "MODEL-X-PRO-R",
            "Model X",
            "Variant Pro",
            "1000000",
            "Red",
        ),
        _row(
            "00000000-0000-0000-0000-000000000021",
            "MODEL-X-PRO-B",
            "Model X",
            "Variant Pro",
            "1005000",
            "Blue",
        ),
    ]
    matches = _unique_booking_matches(
        rows,
        model_name="Model X",
        variant_name="Variant Pro",
        colour_name=None,
        total_commercial_amount=Decimal(1000000),
    )
    assert len(matches) == 2

    candidates = rank_sku_candidates(
        matches,
        model_name="Model X",
        variant_name="Variant Pro",
        total_commercial_amount=Decimal(1000000),
        max_candidates=5,
        tentative=True,
    )
    assert len(candidates) == 2
    assert all(item.candidateStatus == "TENTATIVE" for item in candidates)
    assert all(item.confirmationRequired is True for item in candidates)
    assert all(item.displayLabel.endswith(" *") for item in candidates)


def test_variant_and_colour_can_narrow_multiple_model_price_rows() -> None:
    rows = [
        _row(
            "00000000-0000-0000-0000-000000000030",
            "MODEL-X-PRO-R",
            "Model X",
            "Variant Pro",
            "1000000",
            "Red",
        ),
        _row(
            "00000000-0000-0000-0000-000000000031",
            "MODEL-X-BASE-B",
            "Model X",
            "Variant Base",
            "1000000",
            "Blue",
        ),
    ]
    matches = _unique_booking_matches(
        rows,
        model_name="Model X",
        variant_name="Variant Pro",
        colour_name="Red",
        total_commercial_amount=Decimal(1000000),
    )
    assert len(matches) == 1
    assert matches[0]["sku_code"] == "MODEL-X-PRO-R"


def test_price_outside_direct_tolerance_falls_back_to_tentative_ranking() -> None:
    rows = [
        _row(
            "00000000-0000-0000-0000-000000000040",
            "XUV700-AX7L-R",
            "XUV700",
            "AX7 L",
            "2510000",
            "Red Rage",
        )
    ]
    strict = _unique_booking_matches(
        rows,
        model_name="XUV700",
        variant_name="AX7 L",
        colour_name=None,
        total_commercial_amount=Decimal(2400000),
    )
    assert strict == []

    candidates = rank_sku_candidates(
        rows,
        model_name="XUV700",
        variant_name="AX7 L",
        total_commercial_amount=Decimal(2400000),
        max_candidates=5,
    )
    assert candidates
    assert candidates[0].candidateStatus == "TENTATIVE"


def test_unrelated_low_score_rows_are_not_returned_as_false_candidates() -> None:
    rows = [
        _row(
            "00000000-0000-0000-0000-000000000050",
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
        total_commercial_amount=Decimal(2500000),
        max_candidates=5,
    )
    assert candidates == []


def test_tentative_selection_is_written_as_tentative() -> None:
    connection = _FakeConnection(existing_status=None)
    updated, confirmed_preserved = _persist_sku_selection(
        connection,
        tenant_id="tenant-1",
        journey_id=UUID("00000000-0000-0000-0000-000000000099"),
        candidate=_candidate(tentative=True),
        selection_status="TENTATIVE",
        selection_method="BOOKING_MODEL_PRICE_MULTI_V1",
    )
    assert updated is True
    assert confirmed_preserved is False
    assert len(connection.calls) == 2
    sql, params = connection.calls[1]
    assert "selection_status=EXCLUDED.selection_status" in sql
    assert params["selection_status"] == "TENTATIVE"
    assert params["selection_method"] == "BOOKING_MODEL_PRICE_MULTI_V1"


def test_unique_booking_selection_is_written_as_confirmed() -> None:
    connection = _FakeConnection(existing_status="TENTATIVE")
    updated, confirmed_preserved = _persist_sku_selection(
        connection,
        tenant_id="tenant-1",
        journey_id=UUID("00000000-0000-0000-0000-000000000099"),
        candidate=_candidate(tentative=False),
        selection_status="CONFIRMED",
        selection_method="BOOKING_MODEL_PRICE_UNIQUE_V1",
    )
    assert updated is True
    assert confirmed_preserved is False
    _, params = connection.calls[1]
    assert params["selection_status"] == "CONFIRMED"
    assert params["selection_method"] == "BOOKING_MODEL_PRICE_UNIQUE_V1"


def test_confirmed_sku_is_never_overwritten_by_new_booking_resolution() -> None:
    connection = _FakeConnection(existing_status="CONFIRMED")
    updated, confirmed_preserved = _persist_sku_selection(
        connection,
        tenant_id="tenant-1",
        journey_id=UUID("00000000-0000-0000-0000-000000000099"),
        candidate=_candidate(tentative=True),
        selection_status="TENTATIVE",
        selection_method="BOOKING_MODEL_PRICE_MULTI_V1",
    )
    assert updated is False
    assert confirmed_preserved is True
    assert len(connection.calls) == 1
