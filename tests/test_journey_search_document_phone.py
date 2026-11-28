"""Journey search by a mobile read from the Journey's own papers (product owner 2026-11-28:
the Reviewer's mobile search found nothing -- a Phase 2 customer's number is on the papers,
not on the customer record)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import delete_tenant_data
from p2_support import AllowAllAuthorization, add_ready_document, create_p2_journey, database_engine

from audit_core.uc03_journey_search import search_journeys


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2jsp")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _search(journey, q: str):
    with journey.engine.begin() as connection:
        return search_journeys(tenant_id=journey.tenant_id, human_principal=SimpleNamespace(subject=journey.actor_id),
                               authorization_client=AllowAllAuthorization(role_key="PC"), connection=connection,
                               query=q, limit=10)


def test_a_mobile_on_the_papers_finds_the_journey(journey):
    add_ready_document(journey, "booking_docket", customer_phone="+91 98765-43210")
    for typed in ("9876543210", "+91 98765 43210", "765-43210"):
        result = _search(journey, typed)
        assert [str(i.journeyId) for i in result.items] == [str(journey.journey_id)], typed
    exact = _search(journey, "9876543210").items[0]
    assert exact.matchedOn == "MOBILE_NUMBER" and exact.matchedValue.endswith("3210")
    assert _search(journey, "9123456789").items == []
