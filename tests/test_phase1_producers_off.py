"""Phase 1 task producers are off for Phase 2 Journeys (product owner
2026-10-29): Audit Core's Phase 2 tasks are the only tasks a PC, TL or PM
works, and the legacy rule-engine paths write no findings for them."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

import audit_core.uc03_rule_engine_findings as findings
from audit_core.db import set_tenant_context
from audit_core.workflow import create_workflow_task


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p1off")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _on_phase2(journey) -> None:
    with journey.engine.begin() as connection:
        connection.execute(
            text("INSERT INTO auditcore.p2_journey_runtime (tenant_id, journey_id) VALUES (:t, :j)"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )


def _create(journey, **kwargs):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return create_workflow_task(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            workflow_type="TEST", process_area="BOOKING", task_type="MANUAL_VERIFICATION_REVIEW", **kwargs,
        )


def test_a_phase1_task_is_still_created_for_a_journey_phase2_does_not_run(journey) -> None:
    assert _create(journey, assigned_role_code="PC") is not None


def test_no_phase1_task_for_a_phase2_journey(journey) -> None:
    _on_phase2(journey)
    assert _create(journey, assigned_role_code="PC") is None
    with journey.engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM auditcore.workflow_tasks WHERE tenant_id=:t"), {"t": journey.tenant_id},
        ).scalar_one() == 0


def test_a_command_a_person_runs_and_a_background_job_still_get_their_task(journey) -> None:
    _on_phase2(journey)
    assert _create(journey, assigned_role_code="TL", allow_phase2=True) is not None
    assert _create(journey) is not None  # no assigned role: a background job


def test_the_legacy_rule_engine_path_skips_a_phase2_journey(journey, monkeypatch) -> None:
    _on_phase2(journey)
    monkeypatch.setattr(findings, "build_rule_engine_client", lambda: object())

    def _no_security():
        raise AssertionError("the rule engine must not be asked for a Phase 2 Journey on this path")

    monkeypatch.setattr(findings, "_build_security_oauth_client", _no_security)
    findings.run_rule_engine_phase(journey.engine, journey.tenant_id, journey.journey_id, "BOOKING", "BOOKING", "c")
