from decimal import Decimal
from uuid import UUID

import pytest

from audit_core.errors import AuditCoreError
from audit_core.uc03_sku_candidates import (
    SkuCandidate,
    _commercial_similarity,
    _exact_booking_matches,
    _exact_direct_sku_matches,
    _label_similarity,
    _persist_sku_selection,
    _raise_model_not_found,
    _resolved_candidate,
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
        masterTotalAmount=Decimal(2500000),
        observedTotalCommercialAmount=Decimal(2500000),
        commercialDifferenceAmount=Decimal(0),
        commercialDifferencePercent=Decimal(0),
        score=Decimal(1),
        modelScore=Decimal(1),
        variantScore=Decimal(1),
        commercialScore=Decimal(1),
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


def test_label_match_is_format_normalized_but_not_fuzzy() -> None:
    assert _label_similarity("XUV 700", "XUV700") == Decimal(1)
    assert _label_similarity("AX7-L", "AX7 L") == Decimal(1)
    assert _label_similarity("XUV700", "XUV 700 AX7") == Decimal(0)


def test_commercial_match_has_zero_tolerance() -> None:
    exact, exact_difference, _ = _commercial_similarity(
        Decimal(2500000), Decimal(2500000)
    )
    near, near_difference, _ = _commercial_similarity(
        Decimal(2500000), Decimal(2500001)
    )
    assert exact == Decimal(1)
    assert exact_difference == Decimal(0)
    assert near == Decimal(0)
    assert near_difference == Decimal(1)


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


def test_unique_exact_model_and_price_match_is_resolved() -> None:
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
    matches = _exact_booking_matches(
        rows,
        model_name="Model-X",
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


def test_one_rupee_price_difference_is_not_a_match() -> None:
    rows = [
        _row(
            "00000000-0000-0000-0000-000000000020",
            "MODEL-X-PRO",
            "Model X",
            "Variant Pro",
            "1000000",
            "Red",
        )
    ]
    assert (
        _exact_booking_matches(
            rows,
            model_name="Model X",
            variant_name="Variant Pro",
            colour_name=None,
            total_commercial_amount=Decimal(999999),
        )
        == []
    )


def test_non_exact_model_is_not_a_match_even_when_price_matches() -> None:
    rows = [
        _row(
            "00000000-0000-0000-0000-000000000030",
            "XUV700-AX7L",
            "XUV700",
            "AX7 L",
            "2500000",
        )
    ]
    assert (
        _exact_booking_matches(
            rows,
            model_name="XUV700 AX7",
            variant_name="AX7 L",
            colour_name=None,
            total_commercial_amount=Decimal(2500000),
        )
        == []
    )


def test_multiple_exact_model_price_matches_remain_tentative() -> None:
    rows = [
        _row(
            "00000000-0000-0000-0000-000000000040",
            "MODEL-X-PRO-R",
            "Model X",
            "Variant Pro",
            "1000000",
            "Red",
        ),
        _row(
            "00000000-0000-0000-0000-000000000041",
            "MODEL-X-PRO-B",
            "Model X",
            "Variant Pro",
            "1000000",
            "Blue",
        ),
    ]
    matches = _exact_booking_matches(
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
    assert all(item.commercialDifferenceAmount == Decimal("0.00") for item in candidates)


def test_variant_and_colour_can_narrow_exact_model_price_rows() -> None:
    rows = [
        _row(
            "00000000-0000-0000-0000-000000000050",
            "MODEL-X-PRO-R",
            "Model X",
            "Variant Pro",
            "1000000",
            "Red",
        ),
        _row(
            "00000000-0000-0000-0000-000000000051",
            "MODEL-X-BASE-B",
            "Model X",
            "Variant Base",
            "1000000",
            "Blue",
        ),
    ]
    matches = _exact_booking_matches(
        rows,
        model_name="Model X",
        variant_name="Variant Pro",
        colour_name="Red",
        total_commercial_amount=Decimal(1000000),
    )
    assert len(matches) == 1
    assert matches[0]["sku_code"] == "MODEL-X-PRO-R"


def test_no_exact_match_raises_model_not_found_flag() -> None:
    with pytest.raises(AuditCoreError) as exc_info:
        _raise_model_not_found(
            model_name="XUV700",
            total_commercial_amount=Decimal(2499999),
        )
    error = exc_info.value
    assert error.error_code == "VAC-SKU-001"
    assert error.status_code == 422
    assert error.title == "Model not found in masters"


def test_tentative_selection_is_written_as_tentative() -> None:
    connection = _FakeConnection(existing_status=None)
    updated, confirmed_preserved = _persist_sku_selection(
        connection,
        tenant_id="tenant-1",
        journey_id=UUID("00000000-0000-0000-0000-000000000099"),
        candidate=_candidate(tentative=True),
        selection_status="TENTATIVE",
        selection_method="BOOKING_MODEL_PRICE_MULTI_EXACT_V1",
    )
    assert updated is True
    assert confirmed_preserved is False
    _, params = connection.calls[1]
    assert params["selection_status"] == "TENTATIVE"


def test_unique_exact_booking_selection_is_written_as_confirmed() -> None:
    connection = _FakeConnection(existing_status="TENTATIVE")
    updated, confirmed_preserved = _persist_sku_selection(
        connection,
        tenant_id="tenant-1",
        journey_id=UUID("00000000-0000-0000-0000-000000000099"),
        candidate=_candidate(tentative=False),
        selection_status="CONFIRMED",
        selection_method="BOOKING_MODEL_PRICE_EXACT_V1",
    )
    assert updated is True
    assert confirmed_preserved is False
    _, params = connection.calls[1]
    assert params["selection_status"] == "CONFIRMED"


def test_confirmed_sku_is_never_overwritten() -> None:
    connection = _FakeConnection(existing_status="CONFIRMED")
    updated, confirmed_preserved = _persist_sku_selection(
        connection,
        tenant_id="tenant-1",
        journey_id=UUID("00000000-0000-0000-0000-000000000099"),
        candidate=_candidate(tentative=True),
        selection_status="TENTATIVE",
        selection_method="BOOKING_MODEL_PRICE_MULTI_EXACT_V1",
    )
    assert updated is False
    assert confirmed_preserved is True
    assert len(connection.calls) == 1
