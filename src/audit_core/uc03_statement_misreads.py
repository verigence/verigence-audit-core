"""An NDC amount whose thousands comma, or closing "/-", was read as the digit 1.

Handwritten NDC figures are written with Indian commas ("8,74,808",
"2,20,000", "7,000/-"). Document Intelligence sometimes reads the first comma
as a "1": 8174808, 2120000, 71000. The Deal then takes a discount ten times
the real one onto the Excess discount screen (product owner 2026-11-15,
UTK-SB-261114-012, JASMITA PANI: "NDC is there and it is read wrong..
discount is 7000" -- the NDC's "Other Discount" read 71,000, its "Finance
D/O" 81,74,808 and its "MR No. 1435" 21,20,000).

The closing "/-" of a handwritten figure is read the same way: "7000/-" as
70001, "30,000/-" as 30001 (UTK-SB-261030-009: Scheme 30001, Exchange
Loyalty 20001, Other Discount 70001 -- the printed Total Receipts needs
30,000 + 20,000 + 7,000). Such a figure is tried as the round amount with the
"1" dropped or with the "1" standing for a 0.

The dealer's own printed totals decide it. A copy whose rows do not give its
printed totals is tried with the rows that could be such a misread put back
(the "1" removed where a comma would stand). The repair is taken only when
it makes every printed total add up, and only with the fewest rows changed;
two different repairs that both add up are not chosen between, and the
copy is left as read (STATEMENT_ROWS_DO_NOT_ADD_UP stays with the PC).
A copy that already adds up, or prints no total, is never changed.

Two more places the same misread stands (product owner 2026-11-17: "extend
the existing comma misread repair"), under the same rule:
- every comma of a figure, not only one: "8,74,808" read 81741808 has a "1"
  for each of its two commas;
- a printed total itself ("Net Value", "Total Receipts", "Total Cost",
  "Gross Value"): it is handwritten like the rows and misread like them. A
  total is put back only as the figure its own digits give with the "1"
  removed, and only when the rows then give it -- never worked out from them.
Nothing else is repaired. A digit read as another digit (a 6 as a 3, 10,21,366
as 91,91,366) has no such tell: any row could take the difference, so it
stays with the PC.
"""
from __future__ import annotations

import itertools
from decimal import Decimal
from typing import Any

from audit_core.uc03_account_statement import _amount, line_rows

_AMOUNT_KEYS = ("debit_amount", "credit_amount")
# The printed totals the add-up check reads (uc03_statement_totals), with the
# name each is reported under.
_PRINTED_TOTALS = {"total_cost": "Total Cost", "gross_value": "Gross Value", "net_value": "Net Value",
                   "total_receipts": "Total Receipts"}
PRINTED_TOTAL_SIDE = "printed_total"
# Rows that could be misread beyond this are not tried (the combinations grow fast).
_MAX_SUSPECTS = 8
# A figure has at most this many commas (99,99,99,999).
_MAX_COMMAS = 3
# The repairs tried on one copy before it is left as read. Eight suspects with
# one reading each are 255; this page is read on every Deal load, so the search
# is bounded.
_MAX_TRIALS = 1000


def _comma_positions(digits: str) -> set[int]:
    """Where a comma stands in a whole number written the Indian way
    ("8,74,808": before index 1 and 3) or the Western way ("874,808": 3)."""
    n = len(digits)
    western = {n - k for k in range(3, n, 3)}
    indian = {n - k for k in range(3, n, 2)} if n > 3 else set()
    return {p for p in western | indian if 0 < p < n}


def _round(value: Decimal) -> bool:
    return value >= 100 and value % 100 == 0


def slash_readings(amount: Decimal) -> list[Decimal]:
    """The round amounts this one may be, were its closing "/-" read as a
    "1" (added after the figure, or over its last 0)."""
    if amount <= 0 or amount != amount.to_integral_value() or amount % 10 != 1:
        return []
    out = {amount - 1, (amount - 1) / 10}
    return sorted(v for v in out if v == v.to_integral_value() and _round(v))


def misread_readings(amount: Decimal) -> list[Decimal]:
    """Every amount this one may be, read with a comma or a "/-" as a "1"."""
    return sorted(set(comma_readings(amount)) | set(slash_readings(amount)))


def comma_readings(amount: Decimal) -> list[Decimal]:
    """The amounts this one may be, were one or more of its commas read as a
    "1": every "1" removed stands where a comma of the real figure stands."""
    if amount <= 0 or amount != amount.to_integral_value():
        return []
    digits = str(int(amount))
    ones = [i for i in range(1, len(digits)) if digits[i] == "1"]
    out = set()
    for size in range(1, min(len(ones), _MAX_COMMAS) + 1):
        for removed in itertools.combinations(ones, size):
            real = "".join(d for i, d in enumerate(digits) if i not in removed)
            if real.startswith("0") or len(real) < 4:
                continue
            # the k-th "1" removed stood k places further right than its comma
            # does; two of them never stand for the same comma
            commas = {i - k for k, i in enumerate(removed)}
            if len(commas) == size and commas <= _comma_positions(real):
                out.add(Decimal(real))
    return sorted(out)


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


def _trial(fields: dict[str, Any], rows: list[dict[str, Any]],
           changes: dict[tuple[int, str], Decimal]) -> dict[str, Any]:
    """The copy's fields with these figures put back: a row's amount at
    (its index, its side), a printed total at (-1, its field)."""
    in_rows = {place: value for place, value in changes.items() if place[0] >= 0}
    trial = {**fields, "line_items": _with(rows, in_rows) if in_rows else fields.get("line_items")}
    for (index, key), value in changes.items():
        if index < 0:
            trial[key] = int(value) if isinstance(fields.get(key), int) else str(value)
    return trial


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
                if (amount := _amount(row.get(key))) is not None and (readings := misread_readings(amount))]
    if len(suspects) > _MAX_SUSPECTS:
        return fields, []
    suspects += [((-1, key), readings) for key in _PRINTED_TOTALS
                 if (amount := _amount(fields.get(key))) is not None and (readings := misread_readings(abs(amount)))]
    if not suspects:
        return fields, []
    tried = 0
    for size in range(1, len(suspects) + 1):
        found = []
        for chosen in itertools.combinations(suspects, size):
            for values in itertools.product(*(readings for _, readings in chosen)):
                tried += 1
                if tried > _MAX_TRIALS:
                    return fields, []  # too many ways to try: left as read, with the PC
                changes = {place: value for (place, _), value in zip(chosen, values)}
                trial = _trial(fields, rows, changes)
                if statement_differences(trial) == []:
                    found.append((trial, changes))
        if len(found) == 1:
            trial, changes = found[0]
            repaired = [
                {"row": _PRINTED_TOTALS[key], "side": PRINTED_TOTAL_SIDE,
                 "read": str(_amount(fields.get(key))), "amount": str(value)} if i < 0 else
                {"row": rows[i].get("particulars_raw"), "side": key,
                 "read": str(_amount(rows[i].get(key))), "amount": str(value)}
                for (i, key), value in sorted(changes.items())]
            return trial, repaired
        if found:
            return fields, []
    return fields, []


__all__ = ["comma_readings", "misread_readings", "repair_comma_misreads", "slash_readings"]
