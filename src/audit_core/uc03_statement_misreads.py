"""An NDC amount whose thousands comma was read as the digit 1.

Handwritten NDC figures are written with Indian commas ("8,74,808",
"2,20,000", "7,000/-"). Document Intelligence sometimes reads the first comma
as a "1": 8174808, 2120000, 71000. The Deal then takes a discount ten times
the real one onto the Excess discount screen (product owner 2026-11-15,
UTK-SB-261114-012, JASMITA PANI: "NDC is there and it is read wrong..
discount is 7000" -- the NDC's "Other Discount" read 71,000, its "Finance
D/O" 81,74,808 and its "MR No. 1435" 21,20,000).

The dealer's own printed totals decide it. A copy whose rows do not give its
printed totals is tried with the rows that could be such a misread put back
(the "1" removed where a comma would stand). The repair is taken only when
it makes every printed total add up, and only with the fewest rows changed;
two different repairs that both add up are not chosen between, and the
copy is left as read (STATEMENT_ROWS_DO_NOT_ADD_UP stays with the PC).
A copy that already adds up, or prints no total, is never changed.
"""
from __future__ import annotations

import itertools
from decimal import Decimal
from typing import Any

from audit_core.uc03_account_statement import _amount, line_rows

_AMOUNT_KEYS = ("debit_amount", "credit_amount")
# Rows that could be misread beyond this are not tried (the combinations grow fast).
_MAX_SUSPECTS = 8


def _comma_positions(digits: str) -> set[int]:
    """Where a comma stands in a whole number written the Indian way
    ("8,74,808": before index 1 and 3) or the Western way ("874,808": 3)."""
    n = len(digits)
    western = {n - k for k in range(3, n, 3)}
    indian = {n - k for k in range(3, n, 2)} if n > 3 else set()
    return {p for p in western | indian if 0 < p < n}


def comma_readings(amount: Decimal) -> list[Decimal]:
    """The amounts this one may be, were one of its commas read as a "1"."""
    if amount <= 0 or amount != amount.to_integral_value():
        return []
    digits = str(int(amount))
    out = []
    for i in range(1, len(digits)):
        if digits[i] != "1":
            continue
        real = digits[:i] + digits[i + 1:]
        if real.startswith("0") or len(real) < 4:
            continue
        if i in _comma_positions(real):
            out.append(Decimal(real))
    return sorted(set(out))


def _with(rows: list[dict[str, Any]], changes: dict[tuple[int, str], Decimal]) -> list[dict[str, Any]]:
    out = []
    for index, row in enumerate(rows):
        row = dict(row)
        for key in _AMOUNT_KEYS:
            if (index, key) in changes:
                value = changes[(index, key)]
                row[key] = int(value) if isinstance(row.get(key), int) else str(value)
        out.append(row)
    return out


def repair_comma_misreads(fields: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(the copy's fields with its misread rows put back, the rows put back).
    The fields unchanged and no rows when the copy adds up, is not judged, or
    no single repair makes it add up."""
    from audit_core.uc03_statement_totals import statement_differences

    differences = statement_differences(fields)
    if not differences:
        return fields, []
    rows = line_rows(fields.get("line_items"))
    suspects = [((index, key), readings)
                for index, row in enumerate(rows) for key in _AMOUNT_KEYS
                if (amount := _amount(row.get(key))) is not None and (readings := comma_readings(amount))]
    if not suspects or len(suspects) > _MAX_SUSPECTS:
        return fields, []
    for size in range(1, len(suspects) + 1):
        found = []
        for chosen in itertools.combinations(suspects, size):
            for values in itertools.product(*(readings for _, readings in chosen)):
                changes = {place: value for (place, _), value in zip(chosen, values)}
                trial = {**fields, "line_items": _with(rows, changes)}
                if statement_differences(trial) == []:
                    found.append((trial, changes))
        if len(found) == 1:
            trial, changes = found[0]
            repaired = [{"row": rows[i].get("particulars_raw"), "side": key,
                         "read": str(_amount(rows[i].get(key))), "amount": str(value)}
                        for (i, key), value in sorted(changes.items())]
            return trial, repaired
        if found:
            return fields, []
    return fields, []


__all__ = ["comma_readings", "repair_comma_misreads"]
