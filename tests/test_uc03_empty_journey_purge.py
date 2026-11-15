"""Empty journeys are deleted after 7 days (product owner 2026-11-15); on a real Postgres."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import add_ready_document, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_empty_journey_purge import empty_journeys


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine)
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _age(journey, days: int) -> str:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(
            "UPDATE auditcore.journeys SET created_at_utc = now() - make_interval(days => :d) "
            "WHERE tenant_id=:t AND journey_id=:j RETURNING journey_reference"),
            {"d": days, "t": journey.tenant_id, "j": journey.journey_id}).scalar_one()


def test_an_empty_journey_is_kept_for_seven_days_then_selected(journey):
    reference = _age(journey, 3)
    assert empty_journeys(journey.engine, tenant_id=journey.tenant_id) == []
    _age(journey, 8)
    assert empty_journeys(journey.engine, tenant_id=journey.tenant_id) == [reference]
    assert empty_journeys(journey.engine, tenant_id=journey.tenant_id, references=["OTHER-1"]) == []


def test_a_journey_with_a_document_is_never_empty(journey):
    _age(journey, 30)
    add_ready_document(journey, "booking_form")
    assert empty_journeys(journey.engine, tenant_id=journey.tenant_id) == []
