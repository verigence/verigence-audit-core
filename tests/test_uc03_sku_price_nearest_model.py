"""The ex-showroom price names which MODEL the words meant, when they fit
several (product owner, 2026-10-23: "ex-showroom price is only when there are
multiple models coming in"). It never pins the SKU and never chooses a variant
within one model."""
from __future__ import annotations

from decimal import Decimal

from audit_core.uc03_model_resolution import _suggest_nearest_model_by_price
from audit_core.uc03_sku_words import WordResult


def _row(model: str, sku: str, price: str) -> dict:
    return {"model_name": model, "sku_code": sku, "master_ex_showroom": Decimal(price)}


def _ambiguous(*rows: dict) -> WordResult:
    return WordResult(status="AMBIGUOUS", row=None, candidates=list(rows), detail={})


def test_the_price_names_the_model_when_the_words_fit_several() -> None:
    result = _ambiguous(
        _row("BOLERO NEO", "NEO::N10", "1000000"),
        _row("BOLERO NEO PLUS", "NEOP::P10", "1400000"),
    )
    _suggest_nearest_model_by_price(result, {"exShowroom": Decimal(1395000)})

    assert result.detail["priceNearestModel"] == "BOLERO NEO PLUS"
    assert result.detail["priceNearestSku"] == "NEOP::P10"
    assert [r["sku_code"] for r in result.candidates] == ["NEOP::P10"]
    # A suggestion, never a pin: the PC still confirms.
    assert result.status == "AMBIGUOUS"


def test_the_price_never_chooses_a_variant_inside_one_model() -> None:
    """The module's standing rule: price never picks a variant, fuel, gearbox,
    registration or EV/ICE. With one model there is no model to choose."""
    result = _ambiguous(
        _row("THAR ROXX", "ROXX::MX1", "1000000"),
        _row("THAR ROXX", "ROXX::MX3", "1200000"),
    )
    _suggest_nearest_model_by_price(result, {"exShowroom": Decimal(1000001)})

    assert "priceNearestModel" not in result.detail
    assert len(result.candidates) == 2


def test_a_tie_between_two_models_settles_nothing() -> None:
    result = _ambiguous(
        _row("MODEL A", "A::1", "900000"),
        _row("MODEL B", "B::1", "1100000"),
    )
    _suggest_nearest_model_by_price(result, {"exShowroom": Decimal(1000000)})

    assert "priceNearestModel" not in result.detail
    assert len(result.candidates) == 2


def test_no_price_on_the_document_changes_nothing() -> None:
    result = _ambiguous(
        _row("MODEL A", "A::1", "900000"),
        _row("MODEL B", "B::1", "1100000"),
    )
    _suggest_nearest_model_by_price(result, {"exShowroom": None})

    assert "priceNearestModel" not in result.detail
    assert len(result.candidates) == 2


def test_the_narrowed_model_may_still_hold_several_variants() -> None:
    """Then the task lists one model's variants instead of several models'."""
    result = _ambiguous(
        _row("BOL MAXX PUP HD", "MAXX::SX", "900000"),
        _row("BOL MAXX PUP HD", "MAXX::SXI", "920000"),
        _row("BOL MAXX PUP CITY", "CITY::LX", "1400000"),
    )
    _suggest_nearest_model_by_price(result, {"exShowroom": Decimal(905000)})

    assert result.detail["priceNearestModel"] == "BOL MAXX PUP HD"
    assert "priceNearestSku" not in result.detail
    assert len(result.candidates) == 2
