from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.uc03_rule_execution_log import record_execution
from audit_core.uc03_rule_orchestrator import (
    has_already_run,
    rules_for_event,
    runnable_rules_for_event,
)


@pytest.fixture
def orchestrator_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    tenant_id = f"tenant-orc-{uuid4().hex[:10]}"
    journey_id = uuid4()
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        yield c, tenant_id, journey_id
    engine.dispose()


def test_rules_for_event_returns_only_matching_enabled_audit_core_rules(orchestrator_setup) -> None:
    connection, _tenant_id, _journey_id = orchestrator_setup

    delivery_started = rules_for_event(connection, event="DELIVERY_STARTED")
    codes = {r.rule_code for r in delivery_started}
    assert "WF_BOOKING_INCOMPLETE_AT_DELIVERY_START" in codes
    assert all(r.executor == "AUDIT_CORE" for r in delivery_started)

    # DOCUMENT_SYNCED fans out to many rules -- a real query, not a guess.
    document_synced = rules_for_event(connection, event="DOCUMENT_SYNCED")
    assert len(document_synced) > 5
    assert "WRONG_DOCUMENT" in {r.rule_code for r in document_synced}

    # An event nothing declares returns nothing, not an error.
    assert rules_for_event(connection, event="NO_SUCH_EVENT") == ()


def test_has_already_run_true_only_for_non_error_rows(orchestrator_setup) -> None:
    connection, tenant_id, journey_id = orchestrator_setup

    assert has_already_run(
        connection, tenant_id=tenant_id, journey_id=journey_id, rule_code="DL_VIN_RECONCILIATION"
    ) is False

    record_execution(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        rule_code="DL_VIN_RECONCILIATION",
        triggering_event="DELIVERY_VEHICLE_OBSERVATION_RECORDED",
        outcome="ERROR",
    )
    assert has_already_run(
        connection, tenant_id=tenant_id, journey_id=journey_id, rule_code="DL_VIN_RECONCILIATION"
    ) is False, "an ERROR row must not count as already run"

    record_execution(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        rule_code="DL_VIN_RECONCILIATION",
        triggering_event="DELIVERY_VEHICLE_OBSERVATION_RECORDED",
        outcome="PASS",
    )
    assert has_already_run(
        connection, tenant_id=tenant_id, journey_id=journey_id, rule_code="DL_VIN_RECONCILIATION"
    ) is True


def test_runnable_rules_excludes_already_run_once_rules(orchestrator_setup) -> None:
    connection, tenant_id, journey_id = orchestrator_setup

    before = runnable_rules_for_event(
        connection, tenant_id=tenant_id, journey_id=journey_id,
        event="DELIVERY_VEHICLE_OBSERVATION_RECORDED",
    )
    assert "DL_VIN_RECONCILIATION" in {r.rule_code for r in before}

    record_execution(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        rule_code="DL_VIN_RECONCILIATION",
        triggering_event="DELIVERY_VEHICLE_OBSERVATION_RECORDED",
        outcome="FAIL",
    )
    after = runnable_rules_for_event(
        connection, tenant_id=tenant_id, journey_id=journey_id,
        event="DELIVERY_VEHICLE_OBSERVATION_RECORDED",
    )
    assert "DL_VIN_RECONCILIATION" not in {r.rule_code for r in after}


def test_runnable_rules_keeps_rerunnable_rules_after_a_run(orchestrator_setup) -> None:
    connection, tenant_id, journey_id = orchestrator_setup

    record_execution(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        rule_code="WRONG_DOCUMENT",
        triggering_event="DOCUMENT_SYNCED",
        outcome="PASS",
    )
    still_runnable = runnable_rules_for_event(
        connection, tenant_id=tenant_id, journey_id=journey_id, event="DOCUMENT_SYNCED"
    )
    assert "WRONG_DOCUMENT" in {r.rule_code for r in still_runnable}
