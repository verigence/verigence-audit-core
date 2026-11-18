"""A Deal line far from its standard is checked by hand (product owner 2026-11-18)."""
from __future__ import annotations

from audit_core.uc03_deal_reading_check import lines_to_verify
from audit_core.uc03_observation_severity import system_findings


def _view(*rows: dict) -> dict:
    return {"categories": [{"components": list(rows)}]}


def test_a_line_three_times_its_standard_or_an_ex_showroom_off_the_price_list_is_checked() -> None:
    view = _view(
        {"key": "ex_showroom_price", "label": "Ex-showroom", "standard": "1038999.42", "current": "10381999"},
        {"key": "accessories_cost", "label": "Accessories", "standard": "5000", "current": "49534918"},
        {"key": "insurance_amount", "label": "Insurance", "standard": "42474.70", "current": "41000"},
        {"key": "road_tax_registration", "label": "RTO", "standard": "238139.97", "standardMaster": "238139.97",
         "current": "178564"},
    )
    assert [line["key"] for line in lines_to_verify(view)] == ["ex_showroom_price", "accessories_cost"]
    assert {f["code"] for f in system_findings(view)} == {"DEAL_LINE_VERIFY"}


def test_an_ex_showroom_25_percent_off_is_checked_and_extra_accessories_are_not() -> None:
    view = _view(
        {"key": "ex_showroom_price", "label": "Ex-showroom", "standard": "1748401.12", "current": "980000"},
        {"key": "accessories_cost", "label": "Accessories", "standard": "20000", "current": "44620"},
        {"key": "fastag_amount", "label": "FASTag", "standard": "500", "current": "600"},
        {"key": "rsa_amount", "label": "RSA", "standard": "2021", "current": "24000", "notTaken": True},
    )
    [line] = lines_to_verify(view)
    assert line["key"] == "ex_showroom_price" and "25%" in line["why"]
