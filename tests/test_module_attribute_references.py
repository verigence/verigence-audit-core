"""A module reached as ``from audit_core import x as y`` must still define what ``y.<name>`` uses.

2026-10-19, live DEV: the unified document review and the delivery review confirm
answered 500 (AttributeError) after an upload, because ``booking_review._scope`` no
longer existed. The 2026-10-07 Booking route cleanup had deleted the helper it
pointed at as "unused" while two routes still called it, and nothing exercised
those routes without a database, so it surfaced twelve days later with a user
waiting. This test reads every module's attribute uses statically and fails the
moment one points at a name that is gone, whether or not a route test exists.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

import audit_core
from audit_core import uc03_booking_review_decisions as booking_review
from audit_core.security import HumanPrincipal

_ROOT = pathlib.Path(audit_core.__file__).parent


def _missing_attribute_references() -> list[str]:
    missing: set[str] = set()
    for path in sorted(_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text())
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "audit_core":
                for imported in node.names:
                    aliases[imported.asname or imported.name] = f"audit_core.{imported.name}"
        if not aliases:
            continue
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in aliases
                and isinstance(node.ctx, ast.Load)
            ):
                continue
            # install_* functions set a "<name>_installed" flag on a module the
            # first time they run and read it before that; it is not a missing name.
            if node.attr.endswith("_installed"):
                continue
            module = importlib.import_module(aliases[node.value.id])
            if not hasattr(module, node.attr):
                missing.add(f"{path.name}:{node.lineno} {node.value.id}.{node.attr}")
    return sorted(missing)


def test_no_module_attribute_is_used_that_the_module_no_longer_defines() -> None:
    assert _missing_attribute_references() == []


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement, parameters=None):
        self.calls.append((str(statement), dict(parameters or {})))


def test_review_scope_returns_the_booking_scope_and_sets_the_actor(monkeypatch) -> None:
    booking_scope = {"journey": "j-1"}
    seen: dict[str, object] = {}

    def fake_booking_scope(connection, *args, **kwargs):
        seen["kwargs"] = kwargs
        return booking_scope

    monkeypatch.setattr(booking_review.booking_capture, "_scope", fake_booking_scope)
    connection = _Connection()

    result = booking_review._scope(
        connection,  # type: ignore[arg-type]
        tenant_id="t-1",
        journey_id="j-1",
        human_principal=HumanPrincipal(subject="user-9"),
        authorization_client=None,
    )

    assert result is booking_scope
    assert seen["kwargs"]["tenant_id"] == "t-1"  # type: ignore[index]
    assert len(connection.calls) == 1
    sql, params = connection.calls[0]
    assert "set_config('app.security_actor_id'" in sql
    assert params == {"actor_id": "user-9"}


def test_review_scope_refuses_a_call_without_a_human_principal(monkeypatch) -> None:
    monkeypatch.setattr(booking_review.booking_capture, "_scope", lambda *a, **k: {})
    with pytest.raises(RuntimeError, match="authenticated human principal"):
        booking_review._scope(_Connection(), tenant_id="t-1")  # type: ignore[arg-type]
