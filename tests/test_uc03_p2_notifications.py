"""The notification bell: messages and one's own document status (product owner 2026-11-16); real Postgres."""
from __future__ import annotations

import json

import pytest
from conftest import delete_tenant_data
from p2_support import add_batch_pages, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_notifications import messages_for, seen_at, upload_status


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
        return upload_status(connection, tenant_id=journey.tenant_id, actor_id=actor or str(journey.actor_id))


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


def _bulk(journey, *, source="WEB", status="DONE", files=(("a.pdf", "AWAITING_UPLOAD", "FAILED"),), hours_ago=0):
    bulk = _sql(journey, "INSERT INTO auditcore.p2_bulk_uploads (tenant_id, dealer_id, outlet_id, uploaded_by_actor_id, status, source, "
                         "folder, updated_at_utc) SELECT j.tenant_id, j.dealer_id, j.outlet_id, :a, :s, :src, 'Folder 1', "
                         "now() - make_interval(hours => :h) FROM auditcore.journeys j WHERE j.tenant_id=:t AND "
                         "j.journey_id=:j RETURNING bulk_id", a=str(journey.actor_id), s=status, src=source, h=hours_ago).scalar_one()
    for name, upload_status_, outcome in files:
        _sql(journey, "INSERT INTO auditcore.p2_bulk_files (tenant_id, bulk_id, original_filename, content_type, object_key, upload_status, "
                      "outcome, detail) VALUES (:t, :b, :n, 'application/pdf', :k, :u, :o, :d)",
             b=bulk, n=name, k=f"k-{name}", u=upload_status_, o=outcome,
             d="Not uploaded before submit" if outcome == "FAILED" else None)
    return bulk


def test_only_what_left_no_record_is_listed_not_pages_with_tasks(journey):
    """Product owner 16 Nov: notify only files or journeys that did not get uploaded -- no record
    created; failed pages and the rest already raise tasks."""
    add_batch_pages(journey, [("booking_form", "READY"), (None, "FAILED"), (None, "EXTRACTING")])
    _not_split(journey, "DOC-20261112-WA0021.pdf")
    status = _status(journey)
    assert (status.upload, status.bulk, status.drive, status.total) == (1, 0, 0, 1)
    [item] = status.items
    assert (item.source, item.name, item.reference is not None) == ("UPLOAD", "DOC-20261112-WA0021.pdf", True)
    assert _status(journey, actor="someone-else").total == 0  # only what the person uploaded


def test_a_file_uploaded_again_is_no_longer_waiting(journey):
    _not_split(journey, "scan.pdf")
    _sql(journey, "INSERT INTO auditcore.p2_upload_batches (tenant_id, batch_id, journey_id, original_filename, "
                  "content_type, size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id, "
                  "created_at_utc) VALUES (:t, gen_random_uuid(), :j, 'scan.pdf', 'application/pdf', 100, 1, 'k2', "
                  "'COMPLETED', :a, now() + interval '1 minute')", a=str(journey.actor_id))
    assert _status(journey).total == 0


def test_bulk_files_not_uploaded_and_a_bulk_never_submitted_are_listed(journey):
    _bulk(journey, files=(("x.pdf", "AWAITING_UPLOAD", "FAILED"), ("y.pdf", "UPLOADED", "NEW_BOOKING")))
    _bulk(journey, status="OPEN", hours_ago=5, files=(("p.pdf", "UPLOADED", "PENDING"), ("q.pdf", "AWAITING_UPLOAD", "PENDING")))
    _bulk(journey, status="OPEN", hours_ago=0, files=(("r.pdf", "UPLOADED", "PENDING"),))  # still being worked on
    _bulk(journey, source="DRIVE", files=(("drive.pdf", "AWAITING_UPLOAD", "FAILED"),))
    status = _status(journey)
    assert (status.bulk, status.drive) == (2, 1)
    by_name = {i.name: i for i in status.items}
    assert by_name["x.pdf"].reason == "Not uploaded before submit"
    assert by_name["Folder 1"].reason.startswith("2 file(s) waiting")
    assert by_name["drive.pdf"].source == "DRIVE"


def test_a_drive_file_set_aside_is_listed_with_its_reason(journey):
    _sql(journey, "INSERT INTO auditcore.p2_drive_files (tenant_id, drive_file_id, file_name, mime_type, pc_actor_id, "
                  "file_status, detail) VALUES (:t, 'd1', 'photo.heic', 'image/heic', :a, 'SET_ASIDE', "
                  "'Not a PDF or image')", a=str(journey.actor_id))
    [item] = _status(journey).items
    assert (item.source, item.name, item.reason) == ("DRIVE", "photo.heic", "Not a PDF or image")


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
