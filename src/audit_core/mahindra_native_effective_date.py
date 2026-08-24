from __future__ import annotations

from datetime import date
from typing import Any


def resolve_native_effective_from(
    validated_rows: list[tuple[int, dict[str, Any], list[str]]],
    fallback: date,
) -> date:
    detected = {
        str(row.get("source_effective_from")).strip()
        for _, row, _ in validated_rows
        if row.get("source_effective_from")
    }
    if len(detected) != 1:
        return fallback
    try:
        return date.fromisoformat(next(iter(detected)))
    except ValueError:
        return fallback


def install_native_effective_date(mahindra_masters_module) -> None:
    original = mahindra_masters_module._stage_import

    def _stage_import(connection, **kwargs):
        kwargs["effective_from"] = resolve_native_effective_from(
            kwargs.get("validated_rows", []),
            kwargs["effective_from"],
        )
        return original(connection, **kwargs)

    mahindra_masters_module._stage_import = _stage_import
