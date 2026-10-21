"""One-off re-send of pages that failed on our side (2026-10-21)."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import add_page, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_page_resend import run_page_resend


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine)
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _set_error(journey, queue_id, error):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET last_error=:e WHERE tenant_id=:t AND queue_id=:q"),
            {"e": error, "t": journey.tenant_id, "q": queue_id},
        )


def _status(journey, queue_id):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT queue_status FROM auditcore.p2_document_queue WHERE tenant_id=:t AND queue_id=:q"),
            {"t": journey.tenant_id, "q": queue_id},
        ).scalar_one()


def test_only_our_failures_are_resent_and_only_on_commit(journey):
    _, ours, _ = add_page(journey, page_number=1, status="FAILED")
    _, blurred, _ = add_page(journey, page_number=2, status="FAILED")
    _, copy_bug, _ = add_page(journey, page_number=3, status="FAILED")
    _, fine, _ = add_page(journey, page_number=4, status="READY")
    _set_error(journey, ours, "RuntimeError (at audit_core/uc03_p2_worker.py:1236 in _ingest_document)")
    _set_error(journey, blurred, "DI_QUALITY_IMAGE_BLUR_SCORE")
    _set_error(journey, copy_bug, "FACTS_COPY_FAILED: ProgrammingError")

    run_page_resend(journey.engine, "resend-test", [journey.tenant_id])
    assert _status(journey, ours) == "FAILED", "a dry run changes nothing"

    run_page_resend(journey.engine, "resend-test-commit", [journey.tenant_id])
    assert _status(journey, ours) == "QUEUED"
    assert _status(journey, blurred) == "FAILED", "a scan-quality refusal is the page's own problem"
    assert _status(journey, copy_bug) == "FAILED"
    assert _status(journey, fine) == "READY"

    run_page_resend(journey.engine, "resend-test-commit", [journey.tenant_id])
    assert _status(journey, ours) == "QUEUED", "a second run finds nothing to do"
