"""The monthly insurance dump and the OD discount grid (product owner 2026-11-16).
Pure: no database. Synthetic rows in the layout of the Mahindra "Policy Details Report"."""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from openpyxl import Workbook, load_workbook

from audit_core import uc03_p2_insurance_dumps as m

HEAD = ["Location", "Dealer", "Insurance Company", "Policy No", "Policy Tenure", "Policy Type", "Insured Name",
        "Customer Phone", "Vehicle Class", "Model", "Variant", "VIN", "Engine No", "Created Date",
        "OD Discount(%).", "Net OD Premium A", "Period of Own Damage Cover start", "Period of Own Damage Cover end",
        "MISP Code", "Plan Type", "Product Type"]


def _csv(rows: list[list[str]]) -> bytes:
    lines = [",".join(HEAD)] + [",".join(r) for r in rows]
    return ("\n".join(lines) + "\n").encode()


def _row(policy: str, vin: str, cls: str, model: str, od: str, plan: str, created: str = "05-Sep-26") -> list[str]:
    return ["BHUBANESWAR", "DEALER", "Insurer Co", policy, "1", "New", "MR. TEST  PERSON", "9876543210", cls, model,
            "VAR", vin, "`ENG123456", created, od, "12000", created, "04-Sep-27", "MIBL/M&M/X/001", plan, "ADDON"]


def test_plan_type_read_back_from_excel_dates() -> None:
    assert m.plan_type("`1-3") == "1-3"
    assert m.plan_type(date(2026, 3, 1)) == "1-3"  # Excel's 1 March
    assert m.plan_type(date(2026, 3, 3)) == "3-3"
    assert m.plan_type(date(2026, 1, 1)) == "1-1"
    assert m.plan_type("2026-03-01 00:00:00") == "1-3"
    assert m.od_years("3-3", 1, None, None) == 3
    assert m.od_years(None, None, date(2026, 9, 1), date(2029, 8, 31)) == 3
    assert m.tenure_of(3) == "3+3" and m.tenure_of(1) == "1Y" and m.tenure_of(None) is None


def test_csv_backticks_duplicates_and_totals() -> None:
    data = _csv([
        _row("`3311/0001/000/00", "`MA1NE2ZTFT6H42243", "PCP  ", "XUV7XO", "65", "`1-3"),
        _row("3311/0001/000/00", "MA1NE2ZTFT6H42243", "PCP", "XUV7XO", "65", "1-3"),  # pasted again
        _row("`3313/0002/000/00", "`MA1RE2TTKT1H69274", "GCV", "MAXX HD", "60", "`1-1"),
        [""] * 15 + ["39712"] + [""] * 5,  # a total line
    ])
    parsed = m.parse_insurance_file("Shivnath.csv", data)
    assert len(parsed.rows) == 2 and parsed.duplicates == 1
    first = parsed.rows[0]
    assert first["policy_number"] == "3311/0001/000/00" and first["vin"] == "MA1NE2ZTFT6H42243"
    assert first["segment"] == "PCP" and first["plan_type"] == "1-3" and first["od_years"] == 1
    assert first["od_discount_pct"] == Decimal(65) and first["mobile"] == "9876543210"
    assert first["policy_date"] == date(2026, 9, 5)


def test_a_file_without_the_columns_is_refused() -> None:
    with pytest.raises(HTTPException):
        m.parse_insurance_file("x.csv", b"Policy No,Name\n1,A\n")


def _grid() -> m.Grid:
    rows = m.validate_grid({
        "segments": [{"segment": "PCP", "tenure": "1 year", "pct": 60}, {"segment": "pcp", "tenure": "3+3", "pct": 80},
                     {"segment": "GCV", "tenure": "1Y", "pct": "60"}, {"segment": "PCV", "tenure": "1Y", "pct": ""}],
        "models": [{"model": "XEV 9S", "pct": 5}, {"model": "Scorpio-N", "tenure": "3+3", "pct": 3}],
    })
    return m.grid_from_rows(rows)


def test_the_grid_limit_is_the_segment_plus_the_model() -> None:
    grid = _grid()
    assert grid.limit("PCP", "1Y", "XUV7XO") == (Decimal("60.00"), Decimal(0), Decimal("60.00"))
    assert grid.limit("PCP", "1Y", "xev 9s")[2] == Decimal("65.00")
    assert grid.limit("PCP", "1Y", "SCORPIO-N")[2] == Decimal("60.00")  # its addition is for 3+3 only
    assert grid.limit("PCP", "3+3", "SCORPIO N")[2] == Decimal("83.00")
    assert grid.limit("PCV", "1Y", "BOLERO")[2] is None  # left empty: no limit


def test_evaluate_over_within_and_no_limit() -> None:
    grid = _grid()
    over = m.evaluate({"segment": "PCP", "od_years": 1, "model": "XUV7XO", "od_discount_pct": Decimal(65)}, grid)
    assert over["status"] == "OVER" and over["excess"] == Decimal(5)
    within = m.evaluate({"segment": "PCP", "od_years": 1, "model": "XEV 9S", "od_discount_pct": Decimal(65)}, grid)
    assert within["status"] == "WITHIN"
    none = m.evaluate({"segment": "PCV", "od_years": 1, "model": "BOLERO", "od_discount_pct": Decimal(50)}, grid)
    assert none["status"] == "NO_LIMIT"
    assert m.evaluate({"segment": "PCP", "od_years": 1, "model": "X", "od_discount_pct": 90}, None)["status"] == "NO_GRID"


def test_a_dealerships_own_grid_replaces_the_common_one() -> None:
    rows = [
        {"dealer_id": None, "limit_kind": "SEGMENT", "segment": "PCP", "tenure": "1Y", "model": None, "pct": 60},
        {"dealer_id": "d1", "limit_kind": "SEGMENT", "segment": "PCP", "tenure": "1Y", "model": None, "pct": 70},
    ]
    common, own = m.grids_for_month(rows)
    assert common is not None and common.limit("PCP", "1Y", None)[2] == Decimal(60)
    assert own["d1"].limit("PCP", "1Y", None)[2] == Decimal(70)


def test_grid_validation() -> None:
    with pytest.raises(HTTPException):
        m.validate_grid({"segments": [{"segment": "PCP", "tenure": "1Y", "pct": 120}]})
    with pytest.raises(HTTPException):
        m.validate_grid({"segments": [{"segment": "PCP", "tenure": "2 years", "pct": 50}]})
    with pytest.raises(HTTPException):
        m.validate_grid({"segments": [{"segment": "PCP", "tenure": "1Y", "pct": 50},
                                      {"segment": "PCP", "tenure": "1", "pct": 55}]})


def test_the_template_reads_back_as_the_grid() -> None:
    data = m.build_grid_template({"segments": [{"segment": "PCP", "tenure": "1Y", "pct": 60}],
                                  "models": [{"model": "XEV 9S", "tenure": None, "pct": 5}]})
    grid = m.validate_grid(m.grid_from_workbook(data))
    assert {"limit_kind": "SEGMENT", "segment": "PCP", "tenure": "1Y", "model": None, "pct": Decimal("60.00")} in grid
    assert any(r["limit_kind"] == "MODEL" and r["model"] == "XEV 9S" and r["tenure"] is None for r in grid)
    assert len([r for r in grid if r["limit_kind"] == "SEGMENT"]) == 1  # the empty cells set no limit


def test_a_grid_keyed_in_excel_by_hand() -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(["Segment", "Model", "Tenure", "OD Discount %"])
    ws.append(["PCP", None, "1 Year", 62])
    ws.append([None, "Thar Roxx", "All", 2])
    out = io.BytesIO()
    wb.save(out)
    grid = m.grid_from_rows(m.validate_grid(m.grid_from_workbook(out.getvalue())))
    assert grid.limit("PCP", "1Y", "THAR ROXX")[2] == Decimal(64)


def test_the_excel_report_has_its_sheets() -> None:
    report = {
        "month": "2026-09", "gridSet": True,
        "dealers": [{"dealerName": "Utkal", "policies": 2, "over": 1, "within": 1, "noLimit": 0, "matched": 1,
                     "selfButDealer": 1}],
        "odOverLimit": [{"dealerName": "Utkal", "policyNumber": "P1", "tenure": "1Y", "odDiscount": 65.0,
                         "allowed": 60.0, "excess": 5.0}],
        "selfButDealer": [{"dealerName": "Utkal", "journeyReference": "J-1", "decidedByLabel": "Confirmed by the PC"}],
        "noLimit": [],
    }
    wb = load_workbook(io.BytesIO(m.build_insurance_excel(report)))
    assert wb.sheetnames == ["Summary", "OD over the limit", "Self vs dealership", "No limit in grid"]
    over = wb["OD over the limit"]
    heads = [c.value for c in over[1]]
    assert over.cell(row=2, column=heads.index("Excess (points)") + 1).value == 5.0
    assert over.cell(row=2, column=heads.index("Tenure") + 1).value == "1 year"


def test_titles_are_dropped_from_the_insured_name() -> None:
    assert m._bare_name("MRS. NIVEDITA  MAHANTY") == "NIVEDITA MAHANTY"
    assert m._bare_name("M/S        SANGAM FILLING STATION") == "SANGAM FILLING STATION"
