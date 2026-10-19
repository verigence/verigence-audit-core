"""The actor must be set for RLS before the Project list is read, as its own statement.

2026-10-19, live DEV: the Project-list statement returned no rows for a person whose
assignment was active and visible to that same actor. The statement set the actor
only inside a CTE and relied on PostgreSQL running that CTE before it applied RLS to
the tables it reads -- an order PostgreSQL does not promise, and which changed with
table statistics after a bulk assign-and-remove. Running the read with the actor set
beforehand returned the Project. These tests need no database: they pin the order
the handler issues its statements in.
"""

from __future__ import annotations

from fastapi import BackgroundTasks

from audit_core.security import HumanPrincipal
from audit_core.uc03_project_context import list_my_projects


class _Result:
    def mappings(self):
        return self

    def __iter__(self):
        return iter(())


class _RecordingConnection:
    """Records every statement and its parameters, in the order they are executed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement, parameters=None):
        self.calls.append((str(statement), dict(parameters or {})))
        return _Result()


def _run(subject: str) -> _RecordingConnection:
    connection = _RecordingConnection()
    list_my_projects(
        BackgroundTasks(),
        HumanPrincipal(subject=subject),
        connection,  # type: ignore[arg-type]
    )
    return connection


def test_actor_is_set_in_its_own_statement_before_the_project_read() -> None:
    connection = _run("user-123")

    assert len(connection.calls) == 2
    first_sql, first_params = connection.calls[0]
    second_sql, _ = connection.calls[1]

    # The first statement does nothing but set the actor, transaction-locally.
    assert "set_config('app.security_actor_id'" in first_sql
    assert ", true)" in first_sql
    assert first_params == {"actor_id": "user-123"}
    assert "business_assignments" not in first_sql
    assert "projects" not in first_sql

    # The read comes second, so RLS can never see an unset actor.
    assert "auditcore.projects" in second_sql
    assert "auditcore.business_assignments" in second_sql


def test_the_actor_is_the_token_subject_and_nothing_else() -> None:
    connection = _run("d09199ba-eb20-4107-adfe-982414ae0dc0")

    assert connection.calls[0][1] == {"actor_id": "d09199ba-eb20-4107-adfe-982414ae0dc0"}
    assert connection.calls[1][1] == {"actor_id": "d09199ba-eb20-4107-adfe-982414ae0dc0"}
