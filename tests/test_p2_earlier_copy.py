"""A copy comes back when the copy that replaced it never became a document (found live 2026-11-01).

ADI-SJ-261026-003, ADI-SU-261019-005: a newer page superseded the booking
form, the ledger, the Aadhaar; the page was merged into a document that was
never read ("Not read within 30 minutes", 23-26 Oct) and voided as a page, so
the booking held no copy at all and showed the document missing."""
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
    created = create_p2_journey(engine, prefix="p2ecb")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _one(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, **params}).all()


def _masked_pan_then_aadhaar(journey):
    """His PAN (masked) and Aadhaar, then a second file whose Aadhaar page
    superseded the first and was merged into a document never read."""
    requirements = fid._requirements(journey)
    first = fid._upload(journey, requirements, "Bikash Kumar Martha Booking .pdf", 0, [
        ("pan_card", "pan_card", {"pan_number": "FJBPM5078P", "pan_name": "BXXXXH KXXXR MXXXXA"}),
        ("aadhaar", "aadhaar", {"aadhaar_number": "4410 2290 8812", "aadhaar_name": "Bikash Kumar Martha"}),
    ])
    again = fid._upload(journey, requirements, "Bikash aadhaar back.pdf", 10, [
        ("aadhaar", "aadhaar", {"aadhaar_number": "4410 2290 8812", "aadhaar_name": "Bikash Kumar Martha"}),
    ])
    unread = fid._merge_into_document(journey, requirements, again["aadhaar:1"], requirement="aadhaar", read=False)
    return requirements, first, again, unread


def test_a_copy_whose_replacement_was_never_read_comes_back(journey, monkeypatch):
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    requirements, first, again, unread = _masked_pan_then_aadhaar(journey)
    assert fid._state(journey, first["aadhaar:2"], again["aadhaar:1"]) == ["SUPERSEDED", "VOIDED"]
    fid._settle(journey)
    assert fid._state(journey, first["aadhaar:2"], again["aadhaar:1"]) == ["ACTIVE", "VOIDED"]
    # Masked PAN, no GSTIN: the Aadhaar on file names him again.
    assert fid._legal_name(journey) == "Bikash Kumar Martha"
    [(capture,)] = _one(journey, "SELECT capture_status FROM auditcore.document_capture_v2_documents "
                                 "WHERE tenant_id=:t AND di_document_id=:d", d=first["aadhaar:2"]["document_id"])
    assert capture == "CLASSIFIED"
    [(subject, details)] = _one(journey, "SELECT subject_id, details FROM auditcore.p2_activity_events "
                                         "WHERE tenant_id=:t AND event_type='EARLIER_COPY_BACK'")
    assert subject == str(first["aadhaar:2"]["document_id"])
    assert details["replacedBy"] == [str(again["aadhaar:1"]["document_id"])] and details["by"] == "p2-worker"
    fid._settle(journey)  # nothing more on the next recompute
    assert len(_one(journey, "SELECT 1 FROM auditcore.p2_activity_events WHERE tenant_id=:t "
                             "AND event_type='EARLIER_COPY_BACK'")) == 1

    # Read later (the PC's Retry): the document replaces the copy the usual
    # way, and the copy stays replaced.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_document_queue SET queue_status='READY', status_reason=NULL "
                                "WHERE tenant_id=:t AND queue_id=:g"), {"t": journey.tenant_id, "g": unread["group_id"]})
        connection.execute(text("UPDATE auditcore.evidence SET association_status='SUPERSEDED', "
                                "void_reason='REPLACED_BY_NEWER_UPLOAD', voided_at_utc=now() "
                                "WHERE tenant_id=:t AND evidence_id=:e"),
                           {"t": journey.tenant_id, "e": first["aadhaar:2"]["evidence_id"]})
        connection.execute(
            text("INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, journey_document_requirement_id, "
                 "di_subject_id, di_document_id, document_type_key, evidence_purpose, process_area, "
                 "association_status, supersedes_evidence_id) VALUES (:t, :j, :c, :r, gen_random_uuid(), :d, "
                 "'aadhaar', 'BOOKING_DOCUMENT', 'BOOKING', 'ACTIVE', :prior)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "c": journey.customer_id,
             "r": requirements["aadhaar"], "d": unread["document_id"], "prior": first["aadhaar:2"]["evidence_id"]})
    fid._settle(journey)
    assert fid._state(journey, first["aadhaar:2"]) == ["SUPERSEDED"]


def test_a_page_of_the_unread_document_itself_stays_as_it_is(journey, monkeypatch):
    """A two-page Aadhaar: page 2 superseded page 1, both went into one
    document never read. Page 1 is not an earlier upload, only part of the
    document that was not read as a whole: it stays superseded."""
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    requirements = fid._requirements(journey)
    pages = fid._upload(journey, requirements, "Aadhaar both sides.pdf", 0, [
        ("aadhaar", "aadhaar", {"aadhaar_name": "Bikash Kumar Martha"}),
        ("aadhaar", "aadhaar", {"aadhaar_number": "4410 2290 8812"}),
    ])
    unread = fid._merge_into_document(journey, requirements, pages["aadhaar:2"], requirement="aadhaar", read=False)
    with journey.engine.begin() as connection:  # page 1 merged into the same document
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_document_queue SET queue_status='MERGED', merged_into_queue_id=:g "
                                "WHERE tenant_id=:t AND di_document_id=:d"),
                           {"t": journey.tenant_id, "g": unread["group_id"], "d": pages["aadhaar:1"]["document_id"]})
    fid._settle(journey)
    assert fid._state(journey, pages["aadhaar:1"], pages["aadhaar:2"]) == ["SUPERSEDED", "VOIDED"]


@pytest.mark.parametrize("replacement", ["removed by the PC", "read", "still being read"])
def test_a_copy_stays_replaced_unless_the_replacement_was_never_read(journey, monkeypatch, replacement):
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    _, first, again, unread = _masked_pan_then_aadhaar(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        if replacement == "removed by the PC":  # the newer copy was a document, the PC removed it
            connection.execute(text("UPDATE auditcore.evidence SET void_reason='P2_DOCUMENT_VOIDED' "
                                    "WHERE tenant_id=:t AND evidence_id=:e"),
                               {"t": journey.tenant_id, "e": again["aadhaar:1"]["evidence_id"]})
        else:
            connection.execute(text("UPDATE auditcore.p2_document_queue SET queue_status=:s WHERE tenant_id=:t "
                                    "AND queue_id=:g"),
                               {"t": journey.tenant_id, "g": unread["group_id"],
                                "s": "SUPPORTING" if replacement == "read" else "EXTRACTING"})
    fid._settle(journey)
    assert fid._state(journey, first["aadhaar:2"]) == ["SUPERSEDED"]
    assert fid._legal_name(journey) == "BXXXXH KXXXR MXXXXA"  # as it stands: the masked PAN only
    assert not _one(journey, "SELECT 1 FROM auditcore.p2_activity_events WHERE tenant_id=:t "
                             "AND event_type='EARLIER_COPY_BACK'")


def test_the_job_lists_the_copies_and_with_commit_asks_for_the_recompute(journey, monkeypatch):
    from audit_core import uc03_p2_earlier_copy as job

    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    _, first, _, _ = _masked_pan_then_aadhaar(journey)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(job.logger, "info", lambda event, **kw: events.append((event, kw)))
    job.run_earlier_copy_back(journey.engine, "copies-2026-11-01", [journey.tenant_id])
    [item] = [kw for event, kw in events if event == "earlier_copy_back_item"]
    assert item["never_read"] == [{"documentType": "aadhaar", "whose": "Bikash Kumar Martha",
                                   "documentId": str(first["aadhaar:2"]["document_id"])}]
    assert item["foreign_file"] == [] and item["commit"] is False
    assert fid._state(journey, first["aadhaar:2"]) == ["SUPERSEDED"]  # a dry run changes nothing
    assert not _one(journey, "SELECT 1 FROM auditcore.p2_work_queue WHERE tenant_id=:t "
                             "AND work_type='STAGE_RECOMPUTE'")

    job.run_earlier_copy_back(journey.engine, "copies-2026-11-01-commit", [journey.tenant_id])
    assert fid._state(journey, first["aadhaar:2"]) == ["SUPERSEDED"]  # the recompute, not the job, acts
    assert _one(journey, "SELECT work_status FROM auditcore.p2_work_queue WHERE tenant_id=:t "
                         "AND work_type='STAGE_RECOMPUTE'") == [("PENDING",)]
    fid._settle(journey)
    assert fid._state(journey, first["aadhaar:2"]) == ["ACTIVE"]
    events.clear()
    job.run_earlier_copy_back(journey.engine, "copies-2026-11-01-commit", [journey.tenant_id])
    finished = next(kw for event, kw in events if event == "earlier_copy_back_finished")
    assert (finished["journeys"], finished["recomputed"]) == (0, 0)  # nothing left
