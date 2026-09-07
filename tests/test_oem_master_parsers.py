"""Unit tests for the OEM native master parsers.

The xlsx parsers are exercised with synthesised workbooks that mirror the OEM's
real layout. The two PDF parsers were validated against the actual Mahindra
Sept'26 source documents; a regression test over those runs only when
``MAHINDRA_FIXTURES_DIR`` points at a directory holding them.
"""
from __future__ import annotations

import os
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook

from audit_core.oem_master_parsers import (
    MasterParseError,
    parse_corporate_policy,
    parse_master,
    parse_price_list,
)

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
