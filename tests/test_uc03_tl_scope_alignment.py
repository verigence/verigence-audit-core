from __future__ import annotations

from typing import Any

import pytest

from audit_core.authorization import AuthorizationError
from audit_core.uc03_tl_scope_alignment import (
    TL_SCOPE_WHERE_SQL,
    require_project_wide_tl_case_scope,
)


class _ScalarResult:
    def __init__(self, value: int | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> int | None:
        return self.value


class _FakeConnection:
    def __init__(self, value: int | None) -> None:
        self.value = value
        self.sql = ""
        self.params: dict[str, Any] = {}

    def execute(self, statement: Any, params: dict[str, Any]) -> _ScalarResult:
        self.sql = str(statement)
        self.params = params
        return _ScalarResult(self.value)


def test_project_wide_tl_assignment_is_accepted() -> None:
    connection = _FakeConnection(1)

    result = require_project_wide_tl_case_scope(
        connection,  # type: ignore[arg-type]
        tenant_id="tenant-1",
        actor_id="tl-actor",
    )

    assert result is None
    assert "business_role_code='TL'" in connection.sql
    assert "dealer_id IS NULL" in connection.sql
    assert "outlet_id IS NULL" in connection.sql
    assert "dealer_id IS NOT NULL" not in connection.sql
    assert connection.params == {"tenant_id": "tenant-1", "actor_id": "tl-actor"}


def test_missing_project_wide_tl_assignment_is_denied() -> None:
    connection = _FakeConnection(None)

    with pytest.raises(AuthorizationError):
        require_project_wide_tl_case_scope(
            connection,  # type: ignore[arg-type]
            tenant_id="tenant-1",
            actor_id="tl-actor",
        )


def test_case_query_scope_is_project_wide_and_keeps_submission_boundary() -> None:
    assert "tl.dealer_id IS NULL" in TL_SCOPE_WHERE_SQL
    assert "tl.outlet_id IS NULL" in TL_SCOPE_WHERE_SQL
    assert "tl.dealer_id=j.dealer_id" not in TL_SCOPE_WHERE_SQL
    assert "bs.capture_completed_at_utc IS NOT NULL" in TL_SCOPE_WHERE_SQL
