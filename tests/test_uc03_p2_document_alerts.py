"""Each PC is told about their own stuck or failed pages (product owner 2026-11-15); on a real Postgres."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import add_batch_pages, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_document_alerts import ALERT_KIND, publish_document_alerts
from audit_core.uc03_p2_tenant_alerts import active_alerts


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine)
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _publish(journey) -> dict:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return publish_document_alerts(connection, tenant_id=journey.tenant_id)


def _alerts(journey, actor: str | None) -> list:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [a for a in active_alerts(connection, tenant_id=journey.tenant_id, held={}, actor_id=actor)
                if a.kind == ALERT_KIND]


def test_the_pc_who_uploaded_a_stuck_page_is_told_and_no_one_else(journey):
    add_batch_pages(journey, [(None, "EXTRACTING"), (None, "FAILED")])
    _sql(journey, "UPDATE auditcore.p2_document_queue SET updated_at_utc = now() - interval '7 hours' "
                  "WHERE tenant_id=:t AND journey_id=:j AND queue_status='EXTRACTING'")
    assert _publish(journey)["published"] == 1
    [alert] = _alerts(journey, str(journey.actor_id))
    assert "1 page still being read after 6 hours" in alert.title and "1 page failed" in alert.title
    assert _alerts(journey, "someone-else") == []


def test_the_alert_goes_once_nothing_is_left(journey):
    add_batch_pages(journey, [(None, "FAILED")])
    _publish(journey)
    _sql(journey, "UPDATE auditcore.p2_document_queue SET retired_at_utc = now() WHERE tenant_id=:t AND journey_id=:j")
    assert _publish(journey)["withdrawn"] == 1
    assert _alerts(journey, str(journey.actor_id)) == []
