from decimal import Decimal
from uuid import UUID

from audit_core.uc03_sku_candidates import (
    _label_similarity,
)


def _row(
    sku_id: str,
    sku_code: str,
    model: str,
    variant: str,
    amount: str,
    colour: str | None = None,
):
    return {
        "product_sku_id": UUID(sku_id),
        "sku_code": sku_code,
        "model_name": model,
        "variant_name": variant,
        "colour_name": colour,
        "master_total_amount": Decimal(amount),
        "same_date_version_count": 1,
    }


class _ExistingResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _WriteResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _FakeConnection:
    def __init__(self, existing_status=None, write_rowcount: int = 1):
        self.existing_status = existing_status
        self.write_rowcount = write_rowcount
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), params))
        if len(self.calls) == 1:
            return _ExistingResult(self.existing_status)
        return _WriteResult(self.write_rowcount)


def test_label_match_is_format_normalized_but_not_fuzzy() -> None:
    assert _label_similarity("XUV 700", "XUV700") == Decimal(1)
    assert _label_similarity("AX7-L", "AX7 L") == Decimal(1)
    assert _label_similarity("XUV700", "XUV 700 AX7") == Decimal(0)

