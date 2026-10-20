"""A database error inside SKU resolution must not take the caller down.

Found live 2026-10-19 (admin SKU run, journey 8c632a40): an error inside
sync_model_resolution left the transaction aborted, so the escalation right
after it failed with InFailedSqlTransaction ("current transaction is
aborted") and the whole attempt -- and anything else the caller had written
in the same transaction -- was lost without a trace. Resolution now runs in
a savepoint: only the attempt is undone, the AUTOMATED_SYNC_FAILURE finding
is raised, and the caller's own writes survive.
"""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_model_resolution as resolution
from audit_core.db import set_tenant_context
from audit_core.uc03_async_sync_tasks import sync_model_resolution_with_escalation


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="skusp")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _failing_inputs(connection, **_kwargs):
    # A real database error, like the statement timeout seen live.
    connection.execute(text("SELECT 1/0"))


def test_a_database_error_is_contained_and_escalated(journey, monkeypatch):
    monkeypatch.setattr(resolution, "_pin_state", _failing_inputs)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        # Something the caller wrote before resolution ran in the same transaction.
        connection.execute(
            text("UPDATE auditcore.journeys SET updated_at_utc=now() WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
        result = sync_model_resolution_with_escalation(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, correlation_id="t",
        )
        assert result["error"] is True
        assert result["errorType"] == "DataError"
        # The transaction is still usable.
        assert connection.execute(text("SELECT 1")).scalar_one() == 1

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        findings = connection.execute(
            text("SELECT finding_status FROM auditcore.audit_findings "
                 "WHERE tenant_id=:t AND journey_id=:j AND rule_key LIKE 'AUTOMATED_SYNC_FAILURE:SKU_RESOLUTION:%'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalars().all()
    assert findings == ["OPEN"]


def test_no_vehicle_model_read_is_a_named_skip(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        result = resolution.sync_model_resolution(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, correlation_id="t",
        )
    assert result == {"skipped": True, "reason": "no_vehicle_model_read"}
