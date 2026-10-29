"""A Reviewer completes a Delivery the rules have not (product owner 2026-10-29)."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from fastapi import HTTPException
from p2_support import (
    AllowAllAuthorization,
    add_ready_document,
    create_p2_journey,
    database_engine,
    principal,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.errors import AuthorizationError
from audit_core.uc03_p2_api import P2CompleteDeliveryCommand, complete_p2_delivery


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2rcd")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _role(journey, role: str) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.business_assignments SET business_role_code=:r "
                                "WHERE tenant_id=:t"), {"r": role, "t": journey.tenant_id})


def _complete(journey, reason="Vehicle photos not taken; delivery confirmed with the outlet"):
    with journey.engine.begin() as connection:
        return complete_p2_delivery(
            tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            command=P2CompleteDeliveryCommand(reason=reason), human_principal=principal(journey),
            authorization_client=AllowAllAuthorization("REVIEWER"), connection=connection)


def _ready_for_delivery(journey) -> None:
    add_ready_document(journey, "rto_challan", process_area="DELIVERY")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text(
            "INSERT INTO auditcore.p2_journey_runtime (tenant_id, journey_id, fact_version, booking_completion_state, "
            "delivery_completion_state, current_stage) VALUES (:t, :j, 1, 'COMPLETE', 'IN_PROGRESS', "
            "'DELIVERY_DOCUMENT_UPLOAD') ON CONFLICT (tenant_id, journey_id) DO UPDATE SET "
            "booking_completion_state='COMPLETE', delivery_completion_state='IN_PROGRESS'"),
            {"t": journey.tenant_id, "j": journey.journey_id})


def _row(journey, sql):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id}).mappings().first()


def test_a_reviewer_completes_the_delivery_with_a_reason(journey):
    _ready_for_delivery(journey)
    _role(journey, "REVIEWER")
    result = _complete(journey)
    assert result["status"] == "DELIVERY_COMPLETE"
    assert result["pendingAtCompletion"]  # what was still missing is recorded
    runtime = _row(journey, "SELECT delivery_completion_state, current_stage FROM auditcore.p2_journey_runtime "
                            "WHERE tenant_id=:t AND journey_id=:j")
    assert runtime["delivery_completion_state"] == "COMPLETE" and runtime["current_stage"] == "DELIVERY_COMPLETE"
    event = _row(journey, "SELECT source_kind, actor_role_snapshot, safe_payload FROM auditcore.journey_workflow_events "
                          "WHERE tenant_id=:t AND journey_id=:j AND event_type='P2_DELIVERY_COMPLETED'")
    assert event["source_kind"] == "HUMAN" and event["actor_role_snapshot"] == "REVIEWER"
    assert "outlet" in event["safe_payload"]["reason"]
    activity = _row(journey, "SELECT details FROM auditcore.p2_activity_events WHERE tenant_id=:t AND journey_id=:j "
                             "AND event_type='DELIVERY_COMPLETED_BY_REVIEWER'")
    assert activity["details"]["completedBy"] == "REVIEWER"
    with pytest.raises(HTTPException) as again:
        _complete(journey)
    assert again.value.status_code == 409


def test_only_a_reviewer_can_complete_it(journey):
    _ready_for_delivery(journey)
    _role(journey, "PC")
    with pytest.raises((HTTPException, AuthorizationError)):
        _complete(journey)


def test_not_before_the_delivery_has_started(journey):
    _role(journey, "REVIEWER")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text(
            "INSERT INTO auditcore.p2_journey_runtime (tenant_id, journey_id, fact_version, booking_completion_state) "
            "VALUES (:t, :j, 1, 'COMPLETE') ON CONFLICT (tenant_id, journey_id) DO UPDATE "
            "SET booking_completion_state='COMPLETE'"), {"t": journey.tenant_id, "j": journey.journey_id})
    with pytest.raises(HTTPException) as refused:
        _complete(journey)
    assert refused.value.status_code == 409 and "has not started" in refused.value.detail
