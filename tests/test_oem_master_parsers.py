"""Unit tests for the OEM native master parsers.

The xlsx parsers are exercised with synthesised workbooks that mirror the OEM's
real layout. The two PDF parsers were validated against the actual Mahindra
Sept'26 source documents; a regression test over those runs only when
``MAHINDRA_FIXTURES_DIR`` points at a directory holding them.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from audit_core.oem_master_parsers import (
    MasterParseError,
    parse_corporate_policy,
    parse_master,
    parse_price_list,
)
from audit_core.oem_price_masters import _build_preview

_PRICE_HEADER = [
    "Sl. No.", "Category", "Model", "Variant", "Trim", "Fuel", "Transmission",
    "Drive", "Seater", "Ex-Showroom Price", "TCS", "Insurance",
    "Ext. Warranty (4th Yr)", "Ext. Warranty (4th & 5th Yr)", "Accessories Kit",
    "RSA (1 Yr)", "FASTag", "Registration\n(Individual / w/o Hyp.)",
    "On-Road Price\n(Individual / w/o Hyp.)", "Registration\n(Corporate / with Hyp.)",
    "On-Road Price\n(Corporate / with Hyp.)", "Source Sheet",
]


def _price_row(sl, model, variant, ex, reg_ind, reg_corp, source="Sheet A", category="PV"):
    tcs, ins, ew4, ew45, acc, rsa, fastag = 100, 200, 300, 400, 500, 60, 40
    onroad_ind = ex + tcs + ins + ew4 + ew45 + acc + rsa + fastag + reg_ind
    onroad_corp = ex + tcs + ins + ew4 + ew45 + acc + rsa + fastag + reg_corp
    return [
        sl, category, model, variant, variant.split()[0], "PETROL", "MT", "2WD", "5",
        ex, tcs, ins, ew4, ew45, acc, rsa, fastag, reg_ind, onroad_ind, reg_corp,
        onroad_corp, source,
    ]


def _price_workbook(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Price List"
    ws.append(_PRICE_HEADER)
    for row in rows:
        ws.append(row)
    ws.append([None] * 22)
    ws.append(["Effective date", None, "w.e.f. 03.09.2026"] + [None] * 19)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_price_list_parses_components_and_reconciles() -> None:
    content = _price_workbook(
        [
            _price_row(1, "THAR ROXX", "MX1 PMT 2WD", 1_000_000, 90_000, 91_500),
            _price_row(2, "THAR ROXX", "MX5 DAT 4WD", 1_800_000, 160_000, 161_500),
        ]
    )
    result = parse_price_list(content)
    assert not result.errors
    assert len(result.price_rows) == 2
    assert result.effective_from_hint is not None
    first = result.price_rows[0]
    assert first.components["EX_SHOWROOM"] == Decimal("1000000.00")
    assert first.components["REGISTRATION_INDIVIDUAL"] == Decimal("90000.00")
    assert first.components["REGISTRATION_CORPORATE"] == Decimal("91500.00")
    # ex + tcs 100 + ins 200 + ew4 300 + ew45 400 + acc 500 + rsa 60 + fastag 40 + reg_ind
    assert first.onroad_individual == Decimal("1091600.00")


def test_price_list_flags_unreconciled_row() -> None:
    good = _price_row(1, "THAR ROXX", "MX1 PMT 2WD", 1_000_000, 90_000, 91_500)
    bad = _price_row(2, "THAR ROXX", "MX5 DAT 4WD", 1_800_000, 160_000, 161_500)
    bad[18] = 999  # tamper the individual on-road total
    result = parse_price_list(_price_workbook([good, bad]))
    assert len(result.price_rows) == 1
    assert any("on-road (individual)" in e for e in result.errors)


def test_price_list_splits_commercial_registration_basis() -> None:
    rows = [
        _price_row(1, "BOLERO", "B4 BS6.2", 800_000, 60_000, 61_500, source="BOLERO NEO (PVT)"),
        _price_row(2, "BOLERO", "B4 BS6.2", 800_000, 64_000, 65_500, source="BOLERO NEO-COM"),
    ]
    result = parse_price_list(_price_workbook(rows))
    assert not result.errors
    bases = sorted(r.registration_basis for r in result.price_rows)
    assert bases == ["COMMERCIAL", "PRIVATE"]


def test_price_list_rejects_foreign_workbook() -> None:
    wb = Workbook()
    wb.active.append(["something", "else"])
    buf = BytesIO()
    wb.save(buf)
    with pytest.raises(MasterParseError):
        parse_price_list(buf.getvalue())


def test_price_list_tolerates_a_currency_glyph_stuck_to_a_header_label() -> None:
    # Confirmed against a real uploaded master (Mahindra Consolidated List,
    # 03-Sep-2026): the Model column's header cell literally reads "Model₹"
    # -- the Rupee symbol stuck directly onto the label, every other header
    # cell unaffected. The strict header-layout check rejected the entire
    # file outright, silently discarding real Trim data (this exact
    # scenario is what left SCORPIO CLASSIC's trim missing from the Modify
    # Model picker for about a week -- see
    # test_price_list_ingest_persists_trim_across_sibling_variants_of_one_
    # model in test_oem_price_masters.py, which proved ingestion itself was
    # fine once a file actually got past this gate). A header cell is a
    # label, never itself a currency value, so tolerating a trailing
    # currency glyph here can't mask a genuine layout mismatch.
    header = list(_PRICE_HEADER)
    header[2] = "Model₹"
    wb = Workbook()
    ws = wb.active
    ws.title = "Price List"
    ws.append(header)
    ws.append(_price_row(1, "THAR ROXX", "MX1 PMT 2WD", 1_000_000, 90_000, 91_500))
    buf = BytesIO()
    wb.save(buf)

    result = parse_price_list(buf.getvalue())
    assert not result.errors
    assert len(result.price_rows) == 1
    assert result.price_rows[0].model_name == "THAR ROXX"


def test_price_list_preview_exposes_the_structured_attributes() -> None:
    # Confirmed live: an admin reviewing the Publish preview for a fresh
    # upload had no way to see Trim/Fuel/Transmission/Drive/Seater at all --
    # not because parsing dropped them (it doesn't), but because the preview
    # sample dict never included them in the first place, only model/
    # variant/category/registrationBasis/prices. The preview table itself
    # renders whatever keys the sample rows carry (AdminOemMastersPage.tsx's
    # SampleTable derives its columns from Object.keys(sample[0])), so
    # backfilling these keys here is the whole fix, no frontend change
    # needed.
    result = parse_price_list(
        _price_workbook([_price_row(1, "THAR ROXX", "MX1 PMT 2WD", 1_000_000, 90_000, 91_500)])
    )
    preview = _build_preview(result, "PRICE_LIST")
    row = preview["sample"][0]
    assert row["trim"] == "MX1"
    assert row["fuel"] == "PETROL"
    assert row["transmission"] == "MT"
    assert row["drive"] == "2WD"
    assert row["seater"] == "5"


def _corporate_workbook() -> bytes:
    wb = Workbook()
    policy = wb.active
    policy.title = "Corporate Policy"
    policy.append(["CORPORATE PRIVILEGE POLICY From 3rd Sept' 26 - 30th Sept' 26"])
    policy.append(["Personal Range of Vehicles"])
    header = [None, "Corporate Category", "THAR ROXX", None, None, "SCORPIO-N", None, None]
    policy.append(header)
    policy.append([None, None, "M&M ", "Dealer", "Total", "M&M ", "Dealer", "Total"])
    policy.append([None, "Cat-B", 0, 0, 0, 1800, 1800, 3600])
    policy.append([None, "Cat-Z (Signature)", 24000, 16000, 40000, 24000, 16000, 40000])

    companies = wb.create_sheet("Companies List")
    companies.append([None, '"Z" SIGNATURE', None, None, None, None, '"B"'])
    companies.append(
        [None, "S. No. ", "Corporate Type", "Corporate Description", "Corporate Code",
         None, "S. No. ", "Corporate Type", "Corporate Description", "Corporate Code"]
    )
    companies.append(
        [None, 1, "CF - M&M Group Companies", "Mahindra & Mahindra Ltd.", 10642,
         None, 1, "C1, C3", "Acme Corp Ltd.", 20001]
    )
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_corporate_policy_matrix_and_company_list() -> None:
    result = parse_corporate_policy(_corporate_workbook())
    assert not result.errors
    # Cat-B THAR ROXX is 0 -> skipped; Cat-B SCORPIO-N and both Cat-Z rows kept
    cats = {(b.privilege_category, b.brand_alias) for b in result.corporate_benefits}
    assert ("B", "SCORPIO-N") in cats
    assert ("Z", "THAR ROXX") in cats
    assert all(b.m_and_m + b.dealer == b.total for b in result.corporate_benefits)

    codes = {c.corporate_code: c for c in result.corporate_companies}
    assert codes["10642"].privilege_category == "Z"
    assert codes["20001"].privilege_category == "B"


def test_corporate_policy_requires_both_sheets() -> None:
    wb = Workbook()
    wb.active.title = "Corporate Policy"
    buf = BytesIO()
    wb.save(buf)
    with pytest.raises(MasterParseError):
        parse_corporate_policy(buf.getvalue())


# ── PDF regression (opt-in) ─────────────────────────────────────────────────────
_FIXTURES = os.environ.get("MAHINDRA_FIXTURES_DIR")


@pytest.mark.skipif(not _FIXTURES, reason="MAHINDRA_FIXTURES_DIR not set")
@pytest.mark.parametrize(
    ("glob", "kind", "min_rows"),
    [
        ("*Consumer_Scheme*.pdf", "CONSUMER_SCHEME", 40),
        ("*Exchange_scheme*.pdf", "EXCHANGE_SCHEME", 30),
    ],
)
def test_pdf_schemes_against_source_documents(glob, kind, min_rows) -> None:
    matches = list(Path(_FIXTURES).glob(glob))
    assert matches, f"no fixture matching {glob}"
    result = parse_master(kind, matches[0].read_bytes())
    assert not result.errors
    assert len(result.discount_rows) >= min_rows
    for row in result.discount_rows:
        assert row.total_customer_offer >= 0


# ── the dealer's per-model price sheets (decision 2026-09-30) ──────────────────
def _dealer_pv_workbook(rows, *, model="VEERO", fuel="Diesel", wef="Price list w.e.f. Dt.03.09.2026") -> bytes:
    """Mirrors the PV/CV layout: one sheet, a MODEL NAME row, a Model & Variant
    header with two registration / on-road pairs (individual, corporate) and
    the sheet's own column-letter formulas."""
    wb = Workbook()
    ws = wb.active
    ws.title = model
    ws.append(["ADITYA MOTORS"])
    ws.append([wef])
    ws.append(["MODEL NAME", None, model, None, "FUEL TYPE", fuel, None, None, None, None, "STATE", "ODISHA"])
    ws.append(["Model & Variant", None, "Ex-showroom Price", "Tax Collection at Source (TCS)", "Insurance",
               "Extended Warranty (4th & 5th Year)", "Accessories Kit", "RSA (1 year)", "Fastag", None,
               "On Road Price - Individual", None, "On Road Price - Corporate", None])
    ws.append([None] * 10 + ["Registration\n without Hypoth", "On Road Price\nwithout\nHypoth",
                             "Registration\n without Hypoth", "On Road Price\nwithout\nHypoth"])
    ws.append([None, None, "(A)", "(B)", "(C)", "(D)", "(E)", "(F)", "(G)", None,
               "(H)", "(I)=(A+B+C+D+E+F+G+H)", "(J)", "(K)=(A+B+C+D+E+F+G+J)"])
    for row in rows:
        ws.append(row)
    ws.append(["NOTE-"])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pv_row(variant, ex, reg_ind, reg_corp, *, ins=34000, tamper=None):
    tcs, ew, acc, rsa, fastag = 0, 0, 5000, 1520, 500
    onroad_ind = ex + tcs + ins + ew + acc + rsa + fastag + reg_ind
    onroad_corp = ex + tcs + ins + ew + acc + rsa + fastag + reg_corp
    row = [variant, None, ex, tcs, ins, ew, acc, rsa, fastag, None, reg_ind, onroad_ind, reg_corp, onroad_corp]
    if tamper is not None:
        row[11] = tamper
    return row


def _dealer_ev_workbook(sheets: dict[str, list[list]]) -> bytes:
    """Mirrors the EV layout: one sheet per model, Essential Accessories in
    place of the kit, no RSA / warranty, registration without and with
    hypothecation."""
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        ws.append([None, "ADITYA MOTORS"])
        ws.append([None, "MODEL NAME", title, "ODISHA", "STATE", "CUTTACK"])
        ws.append([None, "MODEL & VARIANT", "Ex-showroom Price with Charger", "Tax Collection at Source (TCS)",
                   "Insurance", "Essential Accessories", "Fastag", "On Road Price - Individual"])
        ws.append([None] * 7 + ["Registration without Hypothecation", "On Road Price without Hypothecation",
                                "Registration with Hypothecation", "On Road Price with Hypothecation"])
        ws.append([None, None, "(A)", "(B)", "(C)", "(D)", "(E)", "(F)", "(G)=(A+B+C+D+E+F)", "(H)", "(I)=(A+B+C+D+E+H)"])
        for row in rows:
            ws.append(row)
        ws.append([None, "N:B-"])
        ws.append([None, 1, "The buyer is free to insure elsewhere"])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _ev_row(variant, ex):
    tcs, ins, acc, fastag, reg_without, reg_with = round(ex * 0.01), 80000, 25000, 500, 140, 1640
    base = ex + tcs + ins + acc + fastag
    return [None, variant, ex, tcs, ins, acc, fastag, reg_without, base + reg_without, reg_with, base + reg_with]


def test_dealer_pv_sheet_parses_reconciles_and_dates_from_the_sheet() -> None:
    content = _dealer_pv_workbook([
        _pv_row("1.6XXL HD V2", 859_501, 3_632, 3_632),
        _pv_row("1.5XXL SD V2", 839_500, 51_110, 51_110),
    ])
    result = parse_price_list(content, filename="veero_LNT_Price.xlsx")
    assert not result.errors, result.errors
    assert result.meta["layout"] == "DEALER_PER_MODEL" and result.meta["sheets"] == 1
    assert result.effective_from_hint == date(2026, 9, 3) and result.meta["effectiveFromSource"] == "SHEET"
    first, second = result.price_rows
    assert first.model_name == "VEERO" and first.variant_name == "1.6XXL HD V2" and first.fuel == "DIESEL"
    assert first.category == "ICE" and first.registration_basis == "STANDARD" and first.source_sheet == "VEERO"
    assert first.components["EX_SHOWROOM"] == Decimal("859501.00")
    assert first.components["REGISTRATION_INDIVIDUAL"] == Decimal("3632.00")
    assert "EXT_WARRANTY_4TH_YR" not in first.components  # the sheet has no such column
    assert first.onroad_individual == Decimal("904153.00") == first.onroad_corporate
    assert second.components["REGISTRATION_INDIVIDUAL"] == Decimal("51110.00")


def test_dealer_pv_sheet_rejects_a_row_off_its_own_formula() -> None:
    content = _dealer_pv_workbook([
        _pv_row("1.6XXL HD V2", 859_501, 3_632, 3_632),
        _pv_row("1.6XXL SD V2", 839_501, 3_632, 3_632, tamper=999),
    ])
    result = parse_price_list(content)
    assert len(result.price_rows) == 1
    assert any("1.6XXL SD V2" in e and "on-road is 999" in e for e in result.errors)


def test_dealer_ev_workbook_reads_every_model_sheet_and_the_file_name_date() -> None:
    content = _dealer_ev_workbook({
        "BE6": [_ev_row("BE 6 One B59 R18 NCH", 1_890_000), _ev_row("BE 6 Two B59 R19 C7", 2_240_000)],
        "xuv3XO EV": [_ev_row("XUV3XO EV - AX5 FH", 1_389_000)],
    })
    result = parse_price_list(content, filename="EV_PRICE_-01th_Sept26.xlsx")
    assert not result.errors, result.errors
    assert result.meta["sheets"] == 2 and result.meta["models"] == ["BE6", "xuv3XO EV"]
    assert result.effective_from_hint == date(2026, 9, 1) and result.meta["effectiveFromSource"] == "FILENAME"
    by_variant = {r.variant_name: r for r in result.price_rows}
    be6 = by_variant["BE 6 One B59 R18 NCH"]
    assert be6.components["REGISTRATION_INDIVIDUAL"] == Decimal("140.00")
    # decision 2026-10-06: corporate is the individual figure; the with-hypothecation pair is its own field
    assert be6.components["REGISTRATION_CORPORATE"] == Decimal("140.00")
    assert be6.components["REGISTRATION_WITH_HYPO"] == Decimal("1640.00")
    assert be6.components["HYPOTHECATION_CHARGE"] == Decimal("1500.00")
    assert be6.onroad_corporate == be6.onroad_individual
    assert be6.category == "UNSPECIFIED"  # nothing on the sheet says electric
    assert "RSA_1YR" not in be6.components and "ACCESSORIES_KIT" not in be6.components
    assert be6.components["ESSENTIAL_ACCESSORIES"] == Decimal("25000.00")
    ev = by_variant["XUV3XO EV - AX5 FH"]
    assert ev.fuel == "ELECTRIC" and ev.category == "BEV"


def test_dealer_sheet_and_file_name_dates_disagree_is_a_warning() -> None:
    content = _dealer_pv_workbook([_pv_row("1.6XXL HD V2", 859_501, 3_632, 3_632)])
    result = parse_price_list(content, filename="02_Nos_Added_PRICE_-07th__Sept26.xlsx")
    assert result.effective_from_hint == date(2026, 9, 3)
    assert any("file name says 2026-09-07" in w for w in result.warnings)


def test_dealer_sheet_preview_shows_absent_components_as_blank() -> None:
    result = parse_price_list(_dealer_pv_workbook([_pv_row("1.6XXL HD V2", 859_501, 3_632, 3_632)]))
    sample = _build_preview(result, "PRICE_LIST")["sample"][0]
    assert sample["exShowroom"] == "859501.00" and sample["extWarranty4thYr"] is None


def test_a_repeated_vehicle_is_an_error_and_nothing_is_loaded_for_it() -> None:
    content = _dealer_pv_workbook([
        _pv_row("1.5XXL SD V2", 839_500, 51_110, 51_110),
        _pv_row("1.5XXL SD V2", 839_500, 51_110, 51_110),  # the sheet repeats a row verbatim
        _pv_row("1.6XXL SD V2", 839_501, 3_632, 3_632),
        _pv_row("1.6XXL SD V2", 849_501, 3_632, 3_632),  # ... and one with a different price
    ])
    result = parse_price_list(content)
    assert not result.ok
    assert sum("repeats the vehicle" in e for e in result.errors) == 2
    assert any("identical prices" in e for e in result.errors) and any("different prices" in e for e in result.errors)


def _dealer_trim_workbook(rows, *, notes=(), fuel="DISEL", wef="Price list w.e.f. Dt.23.09.2026", variant_column=True) -> bytes:
    """The Thar 3-door layout: the first column is headed TRIM and holds the full variant, a second
    TRIM column holds the trim code, then fuel / transmission / drive / seats; the MODEL NAME label is
    repeated in a merged cell; the fuel is mis-spelt on the sheet."""
    wb = Workbook()
    ws = wb.active
    ws.title = "THAR 3DOOR"
    ws.append(["ADITYA MOTORS"])
    ws.append([wef])
    ws.append(["MODEL NAME", None, "MODEL NAME", None, None, None, None, "NEW THAR 2WD & 4WD", None, "FUEL TYPE", None, fuel])
    if variant_column:
        ws.append(["TRIM", None, "TRIM", "FUEL", "TRANSMISSION", "DRIVE", "SEATER", "Ex-showroom Price", "Tax Collection at Source (TCS)",
                   "Insurance", "Extended Warranty (4th year)", "Extended Warranty (4th & 5th year)", "Accessories Kit",
                   "RSA (1 year)", "Fastag", None, "On Road Price - Individual", None, "On Road Price - Corporate"])
        ws.append([None] * 16 + ["Registration without Hypoth", "On Road Price without Hypoth", "Registration without Hypoth",
                                 "On Road Price without Hypoth"])
        ws.append([None] * 7 + ["(A)", "(B)", "(C)", "(D)", "(E)", "(F)", "(G)", "(H)", None, "(H)", "(I)=(A+B+C+D+E+F+G+H)", "(J)",
                                "(K)=(A+B+C+D+E+F+G+J)"])
    else:
        ws.append(["TRIM", "FUEL", "TRANSMISSION", "DRIVE", "SEATER", "Ex-showroom Price", "Tax Collection at Source (TCS)",
                   "Insurance", "Extended Warranty (4th year)", "Extended Warranty (4th & 5th year)", "Accessories Kit",
                   "RSA (1 year)", "Fastag", "On Road Price - Individual", None, "On Road Price - Corporate"])
        ws.append([None] * 13 + ["Registration without Hypoth", "On Road Price without Hypoth", "Registration without Hypoth",
                                 "On Road Price without Hypoth"])
        ws.append([None] * 5 + ["(A)", "(B)", "(C)", "(D)", "(E)", "(F)", "(G)", "(H)", "(H)", "(I)=(A+B+C+D+E+F+G+H)", "(J)",
                                "(K)=(A+B+C+D+E+F+G+J)"])
    for row in rows:
        ws.append(row)
    ws.append([None, "NOTE-"])
    for note in notes:
        ws.append([None, note])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _trim_row(variant, trim, ex, *, fuel="DIESEL", trans="MT", drive="2WD", seats=4, ew4=17_999, ew45=32_999, reg=103_940, with_variant=True,
              tamper=None):
    tcs, ins, acc, rsa, fastag = 10_320, 45_288, 30_000, 2_021, 500
    onroad = ex + tcs + ins + ew4 + ew45 + acc + rsa + fastag + reg
    if tamper is not None:
        onroad = tamper
    cells = [trim, fuel, trans, drive, seats, ex, tcs, ins, ew4, ew45, acc, rsa, fastag, reg, onroad, reg, onroad]
    return [variant, None, *cells[:1], *cells[1:5], *cells[5:13], None, reg, onroad, reg, onroad] if with_variant else [
        trim, fuel, trans, drive, seats, ex, tcs, ins, ew4, ew45, acc, rsa, fastag, reg, onroad, reg, onroad]


_THAR_NOTES = (
    "1.HYPOTHETICATION CHARGES RS.1500/- APPLICABLE IF VEHICLE REGISTERED IN FINANCE",
    "Minimum Booking Amount Rs.21,000/-",
    "2.PERMIT CHARGES APPLICABLE IN COMMERCIAL REGISTRATION BELOW 7 STR = RS.1700/-",
    "3.TR CHARGES APPLICABLE ON CBC MODELS",
)


def test_trim_headed_sheet_is_read_by_its_labels_with_the_notes_under_the_table() -> None:
    content = _dealer_trim_workbook([
        _trim_row("AXT D MT 2WD 4S HT BS6.2", "AXT", 1_032_000),
        _trim_row("LXT D AT 4WD 4S HT BS6.2", "LXT", 1_799_500, trans="AT", drive="4WD", reg=180_690),
    ], notes=_THAR_NOTES)
    result = parse_price_list(content, filename="Thar_3Door_price.xlsx")
    assert not result.errors, result.errors
    assert result.meta["models"] == ["NEW THAR 2WD & 4WD"]  # not the repeated label
    assert result.effective_from_hint == date(2026, 9, 23)
    first, second = result.price_rows
    assert (first.trim, first.fuel, first.transmission, first.drive, first.seater) == ("AXT", "DIESEL", "MT", "2WD", "4")
    assert second.transmission == "AT" and second.drive == "4WD"
    assert first.variant_name == "AXT D MT 2WD 4S HT BS6.2"
    assert first.components["HYPOTHECATION_CHARGE"] == Decimal("1500.00")
    assert first.components["REGISTRATION_WITH_HYPO"] == Decimal("105440.00")
    assert first.components["MIN_BOOKING_AMOUNT"] == Decimal("21000.00")
    extras = {k: v for k, v in first.components.items() if k.startswith("EXTRA_CHARGE_")}
    assert list(extras.values()) == [Decimal("1700.00")]
    assert "BELOW 7 STR" in first.component_notes[next(iter(extras))]
    assert any("TR CHARGES" in n for n in result.meta["notesWithoutAnAmount"])
    # on-road is the dealer's own figure (both warranty tiers inside it), untouched
    assert first.onroad_individual == Decimal("1275067.00") and first.onroad_corporate == first.onroad_individual


def test_single_trim_column_builds_the_variant_from_its_parts_and_a_blank_cell_is_nil() -> None:
    rows = [
        _trim_row("", "AX7T", 2_199_000, fuel="DIESEL", seats=6, with_variant=False),
        _trim_row("", "AX7T", 2_164_000, fuel="DIESEL", seats=7, ew4=0, ew45=0, with_variant=False),
    ]
    rows[1][8] = None  # the sheet leaves the 4th-year warranty empty for this vehicle
    rows[1][9] = None
    content = _dealer_trim_workbook(rows, variant_column=False)
    result = parse_price_list(content)
    assert not result.errors, result.errors
    six, seven = result.price_rows
    assert six.variant_name == "AX7T DIESEL MT 2WD 6 STR" and six.seater == "6"
    assert seven.variant_name == "AX7T DIESEL MT 2WD 7 STR" and seven.seater == "7"
    assert seven.components["EXT_WARRANTY_4TH_YR"] == Decimal("0.00")
    assert any("empty price cell" in w for w in result.warnings)


def test_a_sheet_without_a_variant_column_or_enough_attributes_is_refused_not_guessed() -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(["MODEL NAME", "X"])
    ws.append(["TRIM", "Ex-showroom Price", "Insurance"])
    ws.append([None, "(A)", "(B)", "(C)"])
    ws.append(["AX", 1_000_000, 40_000])
    buf = BytesIO()
    wb.save(buf)
    with pytest.raises(MasterParseError):
        parse_price_list(buf.getvalue())


def test_hypothecation_columns_that_disagree_with_the_note_are_an_error() -> None:
    row = _ev_row("BE 6 One B59 R18 NCH", 1_890_000)
    content = _dealer_ev_workbook({"BE6": [row]})
    wb = load_workbook(BytesIO(content))
    wb["BE6"].append([None, "1.HYPOTHETICATION CHARGES RS.1800/- APPLICABLE IF VEHICLE REGISTERED IN FINANCE"])
    buf = BytesIO()
    wb.save(buf)
    result = parse_price_list(buf.getvalue())
    assert any("the note says hypothecation is 1800.00 but the columns differ by 1500.00" in e for e in result.errors)


def test_a_row_whose_on_road_is_off_in_a_trim_sheet_is_an_error() -> None:
    content = _dealer_trim_workbook([
        _trim_row("AXT D MT 2WD 4S HT BS6.2", "AXT", 1_032_000),
        _trim_row("LXT D MT 2WD 4S HT BS6.2", "LXT", 1_299_000, tamper=1_579_999),
    ], notes=_THAR_NOTES)
    result = parse_price_list(content)
    assert len(result.price_rows) == 1 and any("LXT D MT 2WD" in e and "on-road is 1579999" in e for e in result.errors)


# ── the dealer's discount grid (fifth master, decision 2026-09-30) ─────────────
def _grid_workbook(rows, parameters=(), *, title="Effective 1st Sep 2026") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Grid for Aug"
    ws.append([None, title])
    ws.append([None, "Model", "Booking Protection", "Agreed Buffer", "Insurance OD %", "Out of Territory"])
    for row in rows:
        ws.append([None, *row])
    ws.append([None, "Parameter", "Notes"])
    for parameter, note in parameters:
        ws.append([None, parameter, note])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_discount_grid_reads_models_values_and_parameters() -> None:
    from audit_core.oem_master_parsers import parse_discount_grid

    result = parse_discount_grid(_grid_workbook(
        [
            ["3XO", "30 days", 7000, 0.6, "Additional 3K"],
            ["SCORPIO N Old", "Out of scope", "Out of scope", "Out of scope", "Out of scope"],
            ["7XO", "60 days", "Nil", 0.5, "Nil"],
            ["PICKUP, MAXX, MAXX HD", "30 days", 7000, 0.5, "Additional 3K"],
        ],
        [("Penalty Amount", "₹30,000 per case for any policy breach"), ("Price List", "All dealerships follow it")],
    ), filename="Odisha_PV__CV_Discount_Grid-1_Aug_2026.xlsx")
    assert not result.errors, result.errors
    assert result.effective_from_hint == date(2026, 9, 1) and result.meta["effectiveFromSource"] == "SHEET"
    rows = {r.model_alias: r for r in result.grid_rows}
    xo = rows["3XO"]
    assert (xo.booking_protection_days, xo.agreed_buffer_amount, xo.insurance_od_percent, xo.out_of_territory_amount) == (
        30, Decimal("7000.00"), Decimal("60.00"), Decimal("3000.00"))
    old = rows["SCORPIO N Old"]
    assert old.in_scope is False and old.booking_protection_days is None and old.agreed_buffer_amount is None
    seven = rows["7XO"]
    assert seven.agreed_buffer_amount == Decimal("0.00") and seven.out_of_territory_amount == Decimal("0.00")
    assert rows["PICKUP, MAXX, MAXX HD"].model_aliases == ["PICKUP", "MAXX", "MAXX HD"]
    assert result.grid_parameters[0] == {"parameter": "Penalty Amount", "note": "₹30,000 per case for any policy breach"}
    assert _build_preview(result, "DISCOUNT_GRID")["sample"][0]["insuranceOdPercent"] == "60.00"


def test_discount_grid_rejects_an_unreadable_cell_and_a_foreign_workbook() -> None:
    from audit_core.oem_master_parsers import parse_discount_grid

    result = parse_discount_grid(_grid_workbook([["3XO", "thirty", 7000, 0.6, "Additional 3K"]]))
    assert result.grid_rows == [] and any("unreadable days" in e for e in result.errors)
    wb = Workbook()
    wb.active.append(["something", "else"])
    buf = BytesIO()
    wb.save(buf)
    with pytest.raises(MasterParseError):
        parse_discount_grid(buf.getvalue())
