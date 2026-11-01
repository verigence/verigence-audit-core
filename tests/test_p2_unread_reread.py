"""Documents whose read never finished are read again from their stored pages (product owner 2026-11-01)."""
from __future__ import annotations

import pytest
import test_p2_file_identity as fid
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2urr")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _rows(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, **params}).all()


def _unread_gst_certificate(journey):
    """Jitendra Behera's file: his PAN (masked) read, and his GST certificate's
    page read alone, then grouped into one document that was never read."""
    requirements = fid._requirements(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        requirements["gst_certificate"] = connection.execute(
            text("INSERT INTO auditcore.journey_document_requirements (tenant_id, journey_id, requirement_key, "
                 "document_type_key, process_area, requirement_level) VALUES (:t, :j, 'gst_certificate', "
                 "'gst_certificate', 'BOOKING', 'OPTIONAL') RETURNING journey_document_requirement_id"),
            {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one()
    docs = fid._upload(journey, requirements, "Jitendra Behera Booking.pdf", 0, [
        ("pan_card", "pan_card", {"pan_number": "ASIPB8600M", "pan_name": "JXXXXXXA BXXXXXA"}),
        ("gst_certificate", "gst_certificate", {"gstin": "21ASIPB8600M1ZY", "organisation_legal_name": "JITENDRA BEHERA"}),
    ])
    unread = fid._merge_into_document(journey, requirements, docs["gst_certificate:2"], requirement="gst_certificate",
                                      read=False)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_document_queue SET template_key='gst_certificate', "
                                "last_error='NOT_READ_IN_30_MIN' WHERE tenant_id=:t AND queue_id=:g"),
                           {"t": journey.tenant_id, "g": unread["group_id"]})
    return requirements, docs, unread


def _run(journey, monkeypatch, tag: str) -> list[dict]:
    from audit_core import uc03_p2_unread_reread as job

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(job.logger, "info", lambda event, **kw: events.append((event, kw)))
    job.run_unread_reread(journey.engine, tag, [journey.tenant_id])
    return [kw for event, kw in events if event in ("unread_reread_item", "unread_reread_finished")]


def test_an_unread_document_is_listed_then_sent_again_once(journey, monkeypatch):
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    _, _, unread = _unread_gst_certificate(journey)
    fid._settle(journey)
    assert fid._legal_name(journey) == "JXXXXXXA BXXXXXA"  # the GST certificate is not on the booking

    *items, finished = _run(journey, monkeypatch, "unread-2026-11-01")
    assert [(i["outcome"], i["document"], i["pages"], i["file_name"]) for i in items] == [
        ("WOULD_SEND", "GST Registration Certificate", [2], "Jitendra Behera Booking.pdf")]
    assert (finished["would_send"], finished["sent"]) == (1, 0)
    assert len(_rows(journey, "SELECT 1 FROM auditcore.p2_document_queue WHERE tenant_id=:t")) == 3  # nothing new

    *items, finished = _run(journey, monkeypatch, "unread-2026-11-01-commit")
    [item] = items
    assert item["outcome"] == "SENT" and finished["sent"] == 1
    [(status, error, reason)] = _rows(journey, "SELECT queue_status, last_error, status_reason FROM "
                                               "auditcore.p2_document_queue WHERE tenant_id=:t AND queue_id=:g",
                                      g=unread["group_id"])
    assert (status, error) == ("CANCELLED", "SUPERSEDED_PAGE")
    assert reason == "Sent again as GST Registration Certificate by Verigence; this copy is replaced."
    [(kind, queued, override, pages)] = _rows(
        journey, "SELECT unit_kind, queue_status, candidate_override, page_numbers FROM auditcore.p2_document_queue "
                 "WHERE tenant_id=:t AND queue_id=:n", n=item["new_queue_id"])
    assert (kind, queued, override, pages) == ("GROUP", "QUEUED", ["gst_certificate"], [2])
    assert _rows(journey, "SELECT work_status FROM auditcore.p2_work_queue WHERE tenant_id=:t "
                          "AND work_type='DOCUMENT_INGEST' AND work_key=:n", n=item["new_queue_id"]) == [("PENDING",)]
    # A second run finds nothing to do: the document is being read again.
    *items, finished = _run(journey, monkeypatch, "unread-2026-11-01-commit")
    assert items == [] and finished["documents"] == 0


def test_a_document_on_file_a_taken_out_file_or_a_bad_scan_is_not_sent_again(journey, monkeypatch):
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    requirements, _, unread = _unread_gst_certificate(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_document_queue SET last_error='DI_QUALITY_IMAGE_BLUR_SCORE' "
                                "WHERE tenant_id=:t AND queue_id=:g"), {"t": journey.tenant_id, "g": unread["group_id"]})
    *items, _ = _run(journey, monkeypatch, "unread-2026-11-01")
    assert items == []
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_document_queue SET last_error='NOT_READ_IN_30_MIN' "
                                "WHERE tenant_id=:t AND queue_id=:g"), {"t": journey.tenant_id, "g": unread["group_id"]})
        connection.execute(text("UPDATE auditcore.p2_upload_batches SET identity_status='FOREIGN', "
                                "identity_enforced_at_utc=now() WHERE tenant_id=:t AND batch_id=:b"),
                           {"t": journey.tenant_id, "b": unread["batch_id"]})
    *items, _ = _run(journey, monkeypatch, "unread-2026-11-01")
    assert items == []
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_upload_batches SET identity_status='BELONGS', "
                                "identity_enforced_at_utc=NULL WHERE tenant_id=:t AND batch_id=:b"),
                           {"t": journey.tenant_id, "b": unread["batch_id"]})
    # Another GST certificate is on file now: nothing is missing.
    fid._upload(journey, requirements, "Jitendra GST again.pdf", 20, [
        ("gst_certificate", "gst_certificate", {"gstin": "21ASIPB8600M1ZY", "organisation_legal_name": "JITENDRA BEHERA"}),
    ])
    *items, _ = _run(journey, monkeypatch, "unread-2026-11-01")
    assert items == []


def test_at_most_the_per_run_number_is_sent(journey, monkeypatch):
    from audit_core import uc03_p2_unread_reread as job

    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    _unread_gst_certificate(journey)
    monkeypatch.setattr(job, "_PER_RUN", 0)
    *items, finished = _run(journey, monkeypatch, "unread-2026-11-01-commit")
    assert [i["outcome"] for i in items] == ["NEXT_RUN"] and finished["left_for_next_run"] == 1
