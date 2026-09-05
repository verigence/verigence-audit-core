"""UC03 reviewed-value normalization at the Audit Core typed-owner boundary.

DI monetary fields are schema-declared numbers and use Indian-currency normalization,
but older/current facts can still arrive as presentation-formatted strings. Review
must preserve the original DI fact while allowing an unmodified, unambiguous monetary
value to reach an existing numeric Audit Core owner.

This module deliberately accepts only numeric scalars or conventional Indian currency
presentation. Arbitrary text such as ``N/A`` or ``No exchange`` remains a validation
error rather than being guessed or converted to zero.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from audit_core import uc03_booking_capture
from audit_core.errors import AuditCoreError

_CURRENCY_PREFIX = re.compile(r"^(?:₹|INR\b|RS(?:\.|\b))\s*", re.IGNORECASE)
_CURRENCY_SUFFIX = re.compile(r"\s*(?:INR\b|RS(?:\.|\b)|/-)\s*$", re.IGNORECASE)
_NUMERIC = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$")

_installed = False
_original_as_decimal: Callable[[Any, str], Decimal] | None = None


def _numeric_validation_error(field_key: str) -> AuditCoreError:
    return AuditCoreError(
        error_code="VAC-VAL-002",
        status_code=422,
        title="Business validation failed",
        detail=f"{field_key} requires a numeric value.",
    )


def reviewed_decimal(value: Any, field_key: str) -> Decimal:
    """Parse a reviewed numeric value without inventing business meaning.

    Accepted inputs:
    - Decimal/int/float numeric scalars (never bool)
    - plain numeric strings
    - numeric strings using Indian thousands separators
    - the same strings with a conventional ₹ / INR / Rs prefix or INR / Rs / ``/-`` suffix

    Everything else fails with the existing VAC-VAL-002 contract.
    """

    if value is None or isinstance(value, bool):
        raise _numeric_validation_error(field_key)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise _numeric_validation_error(field_key) from exc
    if not isinstance(value, str):
        raise _numeric_validation_error(field_key)

    candidate = value.strip()
    if not candidate:
        raise _numeric_validation_error(field_key)

    candidate = _CURRENCY_PREFIX.sub("", candidate, count=1)
    candidate = _CURRENCY_SUFFIX.sub("", candidate, count=1)
    candidate = candidate.strip().replace(",", "")

    if not _NUMERIC.fullmatch(candidate):
        raise _numeric_validation_error(field_key)
    try:
        return Decimal(candidate)
    except InvalidOperation as exc:
        raise _numeric_validation_error(field_key) from exc


def install_uc03_review_value_normalization() -> None:
    """Use strict presentation-aware decimal parsing for UC03 typed materialization."""

    global _installed, _original_as_decimal
    if _installed:
        return
    _original_as_decimal = uc03_booking_capture._as_decimal
    uc03_booking_capture._as_decimal = reviewed_decimal  # type: ignore[assignment]
    _installed = True
