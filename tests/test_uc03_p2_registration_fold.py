"""Road tax and registration are compared as one figure (product owner
2026-10-20: "road tax and registration charges are mapped wrongly and
twice")."""
from __future__ import annotations

from audit_core.uc03_p2_journey360 import _column_total, _fold_registration


def _row(key, *, standard=None, booking=None, billed=None, sources=()):
    return {
        "key": key, "label": key, "opted": None, "standard": standard, "booking": booking, "billed": billed,
        "ledger": None, "quote": None, "effective": None, "effectiveSource": None, "variance": None,
        "bookingVsBilled": None, "flags": ["BELOW_STANDARD"] if standard else [], "sources": list(sources),
    }


def test_the_screenshot_case_is_one_line_that_agrees():
    # Price list registration (the whole RTO amount) 2,34,890; booking form
    # printed 2,44,754 once, read as both the combined figure and registration;
    # the RTO challan split it into road tax 2,42,514 and registration 2,240.
    rows = [
        _row("registration_charges", standard="234890", booking="244754", billed="2240"),
        _row("road_tax_amount", billed="242514"),
        _row("road_tax_registration", booking="244754"),
        _row("green_tax_amount", billed="1000"),
    ]
    folded = _fold_registration(rows, invoice_on_file=True)
    line = folded[0]
    assert line["key"] == "road_tax_registration"
    assert (line["standard"], line["booking"], line["billed"]) == ("234890", "244754", "244754")
    assert line["variance"] == "9864"
    assert "BOOKING_VS_BILLED" not in line["flags"]
    assert "ABOVE_STANDARD" in line["flags"]

    parts = {r["key"]: r for r in folded if r.get("partOf")}
    assert set(parts) == {"road_tax_amount", "registration_charges"}
    assert parts["registration_charges"]["standard"] is None
    assert parts["registration_charges"]["booking"] is None  # the combined figure again
    assert parts["registration_charges"]["billed"] == "2240"
    assert parts["road_tax_amount"]["billed"] == "242514"
    assert not parts["registration_charges"]["flags"]

    counted = [r for r in folded if not r.get("partOf")]
    assert _column_total(counted, "booking") == "244754"
    assert _column_total(counted, "billed") == "245754"  # with the green tax
    assert _column_total(counted, "standard") == "234890"


def test_booking_parts_are_added_when_the_form_prints_them_separately():
    rows = [
        _row("road_tax_amount", booking="150000", billed="150000"),
        _row("registration_charges", standard="152500", booking="2500", billed="2500"),
    ]
    line = _fold_registration(rows, invoice_on_file=True)[0]
    assert (line["standard"], line["booking"], line["billed"], line["variance"]) == (
        "152500", "152500", "152500", "0")
    parts = [r for r in _fold_registration(rows, invoice_on_file=True) if r.get("partOf")]
    assert {r["booking"] for r in parts} == {"150000", "2500"}


def test_a_lone_booking_figure_is_the_whole_amount():
    rows = [_row("registration_charges", standard="160000", booking="158000")]
    folded = _fold_registration(rows, invoice_on_file=False)
    assert folded[0]["booking"] == "158000"
    assert folded[0]["variance"] == "-2000"
    assert folded[1]["booking"] is None


def test_nothing_changes_without_road_tax_or_registration():
    rows = [_row("green_tax_amount", billed="1000")]
    assert _fold_registration(rows, invoice_on_file=True) == rows
