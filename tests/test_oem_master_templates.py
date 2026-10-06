"""The standard upload templates: each is read by its own reader once its sample rows are replaced, and a
template that still has its samples is refused. The first tests need no database."""
from __future__ import annotations

from io import BytesIO

import pytest
from openpyxl import load_workbook

from audit_core.oem_master_parsers import MasterParseError, parse_master
from audit_core.oem_master_templates import (
    TEMPLATE_KINDS,
    TEMPLATE_SAMPLE_MARKER,
    build_template,
)


def _without_samples(content: bytes) -> bytes:
    """What a person does: delete the sample cells (here: blank every cell that carries the marker, and
    drop the sample vehicle rows' marker text from the model-name cells by replacing it with real text)."""
    wb = load_workbook(BytesIO(content))
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and TEMPLATE_SAMPLE_MARKER in cell.value:
                    cell.value = cell.value.replace(f"{TEMPLATE_SAMPLE_MARKER}: ", "").replace(TEMPLATE_SAMPLE_MARKER + " ", "REAL ")
                    cell.value = cell.value.replace(TEMPLATE_SAMPLE_MARKER, "REAL")
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize("kind", TEMPLATE_KINDS)
def test_a_template_with_its_samples_left_in_is_refused(kind: str) -> None:
    content = build_template(kind)
    assert content is not None
    with pytest.raises(MasterParseError, match="sample rows"):
        parse_master(kind, content, filename="template.xlsx")


def test_the_pdf_bulletins_have_no_template() -> None:
    assert build_template("CONSUMER_SCHEME") is None and build_template("EXCHANGE_SCHEME") is None


def _real_price_workbook(edit=None) -> bytes:
    wb = load_workbook(BytesIO(_without_samples(build_template("PRICE_LIST"))))
    ws = wb["Price List"]
    if edit:
        edit(ws)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_the_price_list_template_is_read_by_the_price_reader_once_the_samples_are_real() -> None:
    result = parse_master("PRICE_LIST", _real_price_workbook(), filename="x.xlsx")
    assert result.errors == []
    assert [r.variant_name for r in result.price_rows] == ["REAL variant 1", "REAL variant 2"]
    assert result.meta["layout"] == "VERIGENCE_TEMPLATE" and result.effective_from_hint is None  # the date is typed
    first = result.price_rows[0]
    assert first.model_name == "REAL model" and first.trim == "S1" and first.fuel == "DIESEL"
    assert first.transmission == "MT" and first.drive == "2WD" and first.seater == "5" and first.registration_basis == "STANDARD"
    components = {k: str(v) for k, v in first.components.items()}
    assert components["EX_SHOWROOM"] == "1000000.00" and components["EXT_WARRANTY_4TH_YR"] == "18000.00"
    assert components["HYPOTHECATION_CHARGE"] == "1500.00" and components["REGISTRATION_WITH_HYPO"] == "101500.00"
    assert components["REGISTRATION_CORPORATE"] == "100000.00" and components["MIN_BOOKING_AMOUNT"] == "21000.00"
    assert components["EXTRA_CHARGE_PERMIT_CHARGES"] == "1700.00" and first.component_notes["EXTRA_CHARGE_PERMIT_CHARGES"] == "Permit charges"
    assert str(first.onroad_individual) == "1187500.00"  # no warranty tier in it


def test_the_template_date_cell_is_read_and_a_typed_date_in_the_file_is_the_hint() -> None:
    result = parse_master("PRICE_LIST", _real_price_workbook(lambda ws: ws.__setitem__("B1", "01-09-2026")))
    assert str(result.effective_from_hint) == "2026-09-01"


def test_a_changed_template_is_refused_in_plain_words() -> None:
    def rename(ws) -> None:
        ws["I2"] = "Ex Showroom"

    with pytest.raises(MasterParseError, match="not those of the template"):
        parse_master("PRICE_LIST", _real_price_workbook(rename))

    def drop_meta_version(ws) -> None:
        ws.parent["_meta"]["B2"] = "0"

    with pytest.raises(MasterParseError, match="old version"):
        parse_master("PRICE_LIST", _real_price_workbook(drop_meta_version))


def test_a_price_cell_that_is_not_a_number_is_an_error_naming_the_row() -> None:
    result = parse_master("PRICE_LIST", _real_price_workbook(lambda ws: ws.__setitem__("J3", "abc")))
    assert any("row 3" in e and "TCS must be a number" in e for e in result.errors)


def test_a_repeated_vehicle_is_an_error_naming_both_rows() -> None:
    def repeat(ws) -> None:
        for col in "ABCDEFGHIJKLMNOPQRSTUVWX":
            ws[f"{col}4"] = ws[f"{col}3"].value

    result = parse_master("PRICE_LIST", _real_price_workbook(repeat))
    assert any("row 4" in e and "repeats the vehicle on row 3" in e for e in result.errors)


def test_an_on_road_price_that_does_not_add_up_names_the_row() -> None:
    result = parse_master("PRICE_LIST", _real_price_workbook(lambda ws: ws.__setitem__("T3", 1_190_000)))
    assert any("row 3" in e and "add up" in e for e in result.errors)
    assert [r.row_no for r in result.price_rows] == [4]


def test_private_and_commercial_insurance_rows_are_two_prices_of_one_vehicle() -> None:
    def commercial(ws) -> None:
        ws["B4"], ws["C4"] = ws["B3"].value, ws["C3"].value
        for col in "DEFGH":
            ws[f"{col}4"] = ws[f"{col}3"].value
        ws["H3"], ws["H4"] = "PRIVATE", "COMMERCIAL"
        # row 4 reuses row 3's other cells but with its own prices already summing: set it to row 3's figures
        for col in "IJKLMNOPQRSTUVWX":
            ws[f"{col}4"] = ws[f"{col}3"].value

    result = parse_master("PRICE_LIST", _real_price_workbook(commercial))
    assert result.errors == []
    assert sorted(r.registration_basis for r in result.price_rows) == ["COMMERCIAL", "PRIVATE"]


def test_the_corporate_policy_template_is_read_by_the_corporate_reader_once_the_samples_are_real() -> None:
    result = parse_master("CORPORATE_POLICY", _without_samples(build_template("CORPORATE_POLICY")))
    assert result.errors == []
    assert len(result.corporate_benefits) == 5 and {b.privilege_category for b in result.corporate_benefits} == {"B", "A", "F", "Y", "Z"}
    assert {c.privilege_category for c in result.corporate_companies} == {"Z", "Y", "F", "A", "B"}
    assert len(result.corporate_companies) == 5


def test_the_discount_grid_template_is_read_by_the_grid_reader_once_the_samples_are_real() -> None:
    result = parse_master("DISCOUNT_GRID", _without_samples(build_template("DISCOUNT_GRID")), filename="grid.xlsx")
    assert result.errors == []
    [row] = result.grid_rows
    assert row.booking_protection_days == 30 and row.in_scope is True
    assert len(result.grid_parameters) == 1
