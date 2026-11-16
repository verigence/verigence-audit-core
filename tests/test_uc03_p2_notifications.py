"""The notification bell: messages and one's own document status (product owner 2026-11-16); real Postgres."""
from __future__ import annotations

import json

import pytest
from conftest import delete_tenant_data
from p2_support import add_batch_pages, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_notifications import document_status, messages_for, seen_at


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2ntf")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _status(journey, actor=None):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return document_status(connection, tenant_id=journey.tenant_id, actor_id=actor or str(journey.actor_id))


def _messages(journey, actor=None):
    actor = actor or str(journey.actor_id)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return messages_for(connection, tenant_id=journey.tenant_id, actor_id=actor, held={},
                            seen=seen_at(connection, tenant_id=journey.tenant_id, actor_id=actor))


def _alert(journey, title, *, audience=None, hours_ago=0, expired=False):
    payload = {"audienceActorIds": audience} if audience else {}
    _sql(journey, "INSERT INTO auditcore.p2_tenant_alerts (tenant_id, alert_kind, alert_status, title, body, payload, "
                  "published_at_utc, expires_at_utc) VALUES (:t, 'MY_DOCUMENTS_ATTENTION', 'PUBLISHED', :title, 'b', "
                  "CAST(:p AS jsonb), now() - make_interval(hours => :h), "
                  "CASE WHEN :x THEN now() - interval '1 minute' ELSE now() + interval '1 day' END)",
         title=title, p=json.dumps(payload), h=hours_ago, x=expired)


def _not_split(journey, name="scan.pdf"):
    _sql(journey, "INSERT INTO auditcore.p2_upload_batches (tenant_id, batch_id, journey_id, original_filename, "
                  "content_type, size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id) "
                  "VALUES (:t, gen_random_uuid(), :j, :n, 'application/pdf', 100, 0, 'k', 'FAILED', :a)",
         n=name, a=str(journey.actor_id))


def test_a_file_never_split_and_failed_pages_are_in_the_status_with_no_time_window(journey):
    """16 Nov: DOC-20261112-WA0021.pdf was never split into pages, so the PC was never alerted;
    a failed page also dropped out of the alert after 24 hours."""
    add_batch_pages(journey, [("booking_form", "READY"), (None, "FAILED"), (None, "EXTRACTING")])
    _sql(journey, "UPDATE auditcore.p2_document_queue SET updated_at_utc = now() - interval '3 days' "
                  "WHERE tenant_id=:t AND journey_id=:j")
    _not_split(journey, "DOC-20261112-WA0021.pdf")
    status = _status(journey)
    assert (status.beingRead, status.stuck, status.failedPages, status.filesNotRead) == (1, 1, 1, 1)
    assert status.needsAction == 2
    [line] = status.journeys
    assert line.files == ["DOC-20261112-WA0021.pdf"]
    assert _status(journey, actor="someone-else").journeys == []  # only what the person uploaded


def test_a_file_uploaded_again_is_no_longer_waiting(journey):
    _not_split(journey, "scan.pdf")
    _sql(journey, "INSERT INTO auditcore.p2_upload_batches (tenant_id, batch_id, journey_id, original_filename, "
                  "content_type, size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id, "
                  "created_at_utc) VALUES (:t, gen_random_uuid(), :j, 'scan.pdf', 'application/pdf', 100, 1, 'k2', "
                  "'COMPLETED', :a, now() + interval '1 minute')", a=str(journey.actor_id))
    assert _status(journey).filesNotRead == 0


def test_an_open_upload_again_task_counts_until_it_is_closed(journey):
    add_batch_pages(journey, [("booking_form", "READY")])
    _sql(journey, "INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, "
                  "source_type, source_code, dedupe_key, title, description, reference, severity, priority, "
                  "assigned_role_code, allowed_actions, completion_protocol, task_status) VALUES (:t, :j, "
                  "'PC_PAGE_QUALITY_REJECTED', 'DOCUMENT_REUPLOAD', 'SYSTEM', 'DOCUMENT', 'DOCUMENT_QUALITY_REJECTED', "
                  "'k1', 'Upload again', 'd', '{}'::jsonb, 'HIGH', 'HIGH', 'PC', '[]'::jsonb, 'MACHINE_VERIFIED', "
                  "'READY')")
    assert _status(journey).uploadTasks == 1
    _sql(journey, "UPDATE auditcore.p2_tasks SET task_status='VERIFIED_COMPLETE' WHERE tenant_id=:t")
    assert _status(journey).uploadTasks == 0


def test_messages_stay_after_they_expire_and_are_unread_until_the_bell_is_opened(journey):
    me = str(journey.actor_id)
    _alert(journey, "Mine, expired", audience=[me], hours_ago=30, expired=True)
    _alert(journey, "For everyone", hours_ago=1)
    _alert(journey, "Someone else's", audience=["other"])
    _alert(journey, "Too old", hours_ago=24 * 8)
    messages = _messages(journey)
    assert [m.title for m in messages] == ["For everyone", "Mine, expired"]
    assert all(m.unread for m in messages)
    _sql(journey, "INSERT INTO auditcore.p2_notification_seen (tenant_id, actor_id) VALUES (:t, :a)", a=me)
    assert not any(m.unread for m in _messages(journey))
    _alert(journey, "New since")
    assert [m.title for m in _messages(journey) if m.unread] == ["New since"]
