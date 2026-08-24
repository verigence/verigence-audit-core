from datetime import date

from audit_core.mahindra_native_effective_date import resolve_native_effective_from


def test_native_workbook_wef_overrides_manual_date() -> None:
    rows = [
        (2, {"source_effective_from": "2026-08-04"}, []),
        (3, {"source_effective_from": "2026-08-04"}, []),
    ]

    assert resolve_native_effective_from(rows, date(2026, 8, 24)) == date(2026, 8, 4)


def test_manual_date_remains_for_generated_template() -> None:
    rows = [(2, {"sku_code": "SKU-1"}, [])]

    assert resolve_native_effective_from(rows, date(2026, 8, 24)) == date(2026, 8, 24)
