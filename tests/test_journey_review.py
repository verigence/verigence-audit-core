"""The all-Journey review merges the per-Journey queries on the reference and
nests invoices; the SQL carries no stray bind parameters."""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any

from audit_core import uc03_journey_review as review


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> list[dict[str, Any]]:
        return self._rows


class _Connection:
    def __init__(self, by_sql: dict[str, list[dict[str, Any]]]) -> None:
        self._by_sql = by_sql
        self.statements: list[str] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        return _Result(self._by_sql.get(sql, []))


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    @contextmanager
    def begin(self):
        yield self._connection


def test_review_sql_only_binds_the_tenant() -> None:
    for name, sql in review.REVIEW_SQL.items():
        binds = set(re.findall(r"(?<![:\w]):([a-z_]+)", sql))
        assert binds == {"t"}, (name, binds)


def test_merges_per_journey_rows_and_nests_invoices(monkeypatch) -> None:
    monkeypatch.setattr(review, "set_tenant_context", lambda connection, tenant_id: None)
    monkeypatch.setattr(review, "text", lambda sql: sql)
    by_sql = {
        review.REVIEW_SQL["journey"]: [
            {"ref": "J-2", "sku_code": "S2"}, {"ref": "J-1", "sku_code": "S1"},
        ],
        review.REVIEW_SQL["insurance"]: [{"ref": "J-1", "insurer_name": "ACME"}],
        review.REVIEW_SQL["invoices"]: [
            {"ref": "J-1", "doc": "aa", "gross": "100"}, {"ref": "J-1", "doc": "bb", "gross": "90"},
        ],
    }
    connection = _Connection(by_sql)
    out = review.journey_review(_Engine(connection), "tenant-1")  # type: ignore[arg-type]

    assert connection.statements[0] == "SET TRANSACTION READ ONLY"
    assert out["journeyCount"] == 2
    first, second = out["journeys"]
    assert (first["ref"], first["sku_code"], first["insurer_name"]) == ("J-1", "S1", "ACME")
    assert [i["doc"] for i in first["invoices"]] == ["aa", "bb"]
    assert second["invoices"] == []
