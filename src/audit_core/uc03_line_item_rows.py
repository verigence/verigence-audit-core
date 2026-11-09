"""One line of a document's line items, corrected on its own (product owner 2026-11-09).

DI reads a table on a document (the NDC's charges and payments, a ledger's
vouchers, an invoice's lines) as one array field. Correcting one row used to
mean retyping the whole array as text. Here a row is changed, added or
removed on its own; the result is written back as the field's new value
through the same correction path as any other field, so the change log, the
TL / Reviewer information and the re-run of the checks are unchanged, and
every check keeps reading one list of rows.

A row change names the row by its position and carries the row as the person
saw it. If the row at that position is no longer that row (someone else
changed the table meanwhile), the change is refused rather than applied to
the wrong line.
"""
from __future__ import annotations

import ast
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

RowOperation = Literal["UPDATE", "ADD", "REMOVE"]

MAX_ROWS = 400
MAX_COLUMNS = 40
MAX_KEY_LENGTH = 60
MAX_TEXT_LENGTH = 500

# Columns that carry a figure: a value typed into them is kept as a number
# when it reads as one ("1,92,000" -> 192000), as DI stores them.
_NUMERIC_KEY = re.compile(r"(AMOUNT|RATE|QUANTITY|QTY|BALANCE|TOTAL|VALUE|PRICE|TAX|CESS|DEBIT|CREDIT)", re.IGNORECASE)
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class RowChangeError(ValueError):
    """The change cannot be applied as asked (HTTP 422)."""


class RowConflictError(ValueError):
    """The row is no longer what the person saw (HTTP 409)."""


def parse_rows(value: Any) -> list[dict[str, Any]] | None:
    """The value as a list of row dicts, whatever shape it was stored in (a
    JSON array, the JSON text of one, or the Python repr older extractions
    left behind). None when the value is not a table at all."""
    raw = value
    if raw is None:
        return None
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped.startswith("["):
            return None
        try:
            raw = json.loads(stripped)
        except (ValueError, TypeError):
            try:
                raw = ast.literal_eval(stripped)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                return None
    if not isinstance(raw, list):
        return None
    if not all(isinstance(row, dict) for row in raw):
        return None
    return [dict(row) for row in raw]


def _as_number(text_value: str) -> int | float | None:
    cleaned = re.sub(r"\s*/\s*[-=]*\s*$", "", text_value.replace(",", "").replace("₹", "").strip())
    if not cleaned:
        return None
    try:
        number = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    if number == number.to_integral_value():
        return int(number)
    return float(round(number, 2))


def clean_row(row: Any, *, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """The row as it will be stored: string keys, plain values, figures kept
    as numbers. Blank cells are kept as null so a cleared value is visible."""
    if not isinstance(row, dict) or not row:
        raise RowChangeError("A line needs at least one value.")
    if len(row) > MAX_COLUMNS:
        raise RowChangeError(f"A line can have at most {MAX_COLUMNS} values.")
    previous = previous or {}
    cleaned: dict[str, Any] = {}
    for key, value in row.items():
        key = str(key).strip()
        if not key or len(key) > MAX_KEY_LENGTH or not _KEY.match(key):
            raise RowChangeError(f"'{key}' is not a column name this table can hold.")
        if value is None or (isinstance(value, str) and not value.strip()):
            cleaned[key] = None
            continue
        if isinstance(value, bool):
            cleaned[key] = value
            continue
        if isinstance(value, (int, float)):
            cleaned[key] = value
            continue
        if not isinstance(value, str):
            raise RowChangeError(f"The value for '{key}' must be text or a number.")
        text_value = value.strip()
        if len(text_value) > MAX_TEXT_LENGTH:
            raise RowChangeError(f"The value for '{key}' is longer than {MAX_TEXT_LENGTH} characters.")
        numeric_column = bool(_NUMERIC_KEY.search(key)) or isinstance(previous.get(key), (int, float))
        number = _as_number(text_value) if numeric_column else None
        cleaned[key] = number if number is not None else text_value
    if all(v is None for v in cleaned.values()):
        raise RowChangeError("A line needs at least one value.")
    return cleaned


def _same_row(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Rows are the same line when their values agree, ignoring blank cells
    and the difference between 21000 and "21000"."""
    def norm(row: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, value in row.items():
            if value is None or value == "":
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[str(key)] = str(Decimal(str(value)).normalize())
                continue
            number = _as_number(str(value)) if isinstance(value, str) else None
            out[str(key)] = str(Decimal(str(number)).normalize()) if number is not None else str(value).strip()
        return out
    return norm(a) == norm(b)


def apply_row_change(
    rows: list[dict[str, Any]],
    *,
    operation: RowOperation,
    row_index: int | None,
    expected_row: dict[str, Any] | None,
    row: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None]:
    """Apply one change and return (new rows, the row before, the row after)."""
    new_rows = [dict(r) for r in rows]
    if operation == "ADD":
        if len(new_rows) >= MAX_ROWS:
            raise RowChangeError(f"This table already has {MAX_ROWS} lines.")
        added = clean_row(row)
        position = len(new_rows) if row_index is None else row_index
        if position < 0 or position > len(new_rows):
            raise RowChangeError("That position is outside the table.")
        new_rows.insert(position, added)
        return new_rows, None, added

    if row_index is None or row_index < 0 or row_index >= len(new_rows):
        raise RowConflictError("That line is no longer in the table. Reload the document and try again.")
    current = new_rows[row_index]
    if expected_row is None or not _same_row(current, expected_row):
        raise RowConflictError("That line was changed by someone else. Reload the document and try again.")
    if operation == "REMOVE":
        del new_rows[row_index]
        return new_rows, current, None
    if operation == "UPDATE":
        updated = clean_row(row, previous=current)
        if _same_row(current, updated):
            raise RowChangeError("Nothing changed on this line.")
        new_rows[row_index] = updated
        return new_rows, current, updated
    raise RowChangeError(f"Unknown change {operation}.")


def is_table_field(field_type: str | None, value: Any) -> bool:
    """A field shown and corrected row by row: DI declares it an array and its
    value is a list of rows (or nothing yet)."""
    if (field_type or "").lower() != "array":
        return False
    if value is None or value == "" or value == "null":
        return True
    return parse_rows(value) is not None
