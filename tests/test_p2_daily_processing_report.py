"""Daily processing report (2026-10-20): per PC, per day, from stored data only."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import add_page, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_daily_report import (
    _require_report_role,
    build_daily_processing_report,
)
from audit_core.uc03_p2_runtime import record_activity


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="dailyrep")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _tl(journey) -> str:
    tl = f"tl-{uuid4().hex[:8]}"
    with journey.engine.begin() as c:
        c.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                 "VALUES (:t, :a, 'TL')"),
            {"t": journey.tenant_id, "a": tl},
        )
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text("UPDATE auditcore.journeys SET created_by_actor_id=:a, created_by_display_name='Subhakanta Jena' "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"a": journey.actor_id, "t": journey.tenant_id, "j": journey.journey_id},
        )
    return tl


def _today(journey):
    with journey.engine.begin() as c:
        return c.execute(text("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date")).scalar_one()


def _report(journey, tl):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return build_daily_processing_report(c, tenant_id=journey.tenant_id, actor_id=tl, day=_today(journey))


def test_a_day_of_one_pc(journey):
    tl = _tl(journey)
    _b1, ready_page, _ = add_page(journey, page_number=1, status="READY")
    _b2, waiting_page, _ = add_page(journey, page_number=2, status="RETRY_WAIT")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text("UPDATE auditcore.p2_upload_batches SET page_count=3 WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
        for event_type, details in [
            ("PAGE_RETRY_REQUESTED", {"by": journey.actor_id}),
            ("PAGE_REREAD_REQUESTED", {"by": journey.actor_id}),
            ("PAGE_REREAD_REQUESTED", {"by": "SYSTEM"}),
            ("RECHECK_REQUESTED", {"units": [], "pagesReread": 1}),
            ("P2_WORK_DEAD_LETTER", {"workType": "DOCUMENT_INGEST"}),
        ]:
            record_activity(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id, event_type=event_type,
                            subject_type="DOCUMENT_PAGE", subject_id=str(ready_page), details=details)
        # The page the night sweep asked for was read afterwards.
        c.execute(
            text("UPDATE auditcore.p2_document_queue SET queue_status='SYNCING_TO_AUDIT_CORE' WHERE queue_id=:q"),
            {"q": ready_page},
        )
        c.execute(
            text("UPDATE auditcore.p2_document_queue SET queue_status='READY', extracted_field_count=4 "
                 "WHERE queue_id=:q"),
            {"q": ready_page},
        )
        c.execute(
            text("INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, "
                 "source_type, title, description, dedupe_key, assigned_role_code, task_status, completion_protocol) "
                 "VALUES (:t, :j, 'PC_UPLOAD_STATUS', 'DOCUMENT', 'SYSTEM', 'DOCUMENT', 'Upload again', 'Page 2', :k, "
                 "'PC', 'READY', 'MACHINE_VERIFIED')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": f"k-{uuid4().hex}"},
        )

    report = _report(journey, tl)

    assert len(report["journeys"]) == 1
    row = report["journeys"][0]
    assert row["pcName"] == "Subhakanta Jena"
    assert row["files"] == 2 and row["pages"] == 6
    assert row["documents"]["extracted"] == 1
    assert row["documents"]["waiting"] == 1
    assert row["processingFinishedAt"] is None  # one document still in flight
    assert row["autoRetries"] == 1             # the page that entered RETRY_WAIT
    assert row["pcRetries"] == 1
    assert row["readAgain"] == 1
    assert row["resyncs"] == 1
    assert row["retriesExhausted"] == 1
    assert row["nightRequests"] == 1 and row["nightFixed"] == 1
    assert row["tasksOpened"] == 1 and row["tasksOpenedPc"] == 1
    pc = report["pcs"][0]
    assert pc["journeysUploaded"] == 1 and pc["pagesPerJourney"] == 6.0 and pc["stillProcessing"] == 1

    # Once nothing is in flight the finish time and the total time are given.
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_document_queue SET queue_status='FAILED', last_error='DI_QUALITY_BLUR' "
                       "WHERE queue_id=:q"), {"q": waiting_page})
    row = _report(journey, tl)["journeys"][0]
    assert row["documents"]["failed"] == 1 and row["documents"]["rejectedScans"] == 1
    assert row["processingFinishedAt"] is not None
    assert row["processingMinutes"] is not None and row["processingMinutes"] >= 0
    assert row["processingFinishedAt"] <= datetime.now(UTC)


def test_only_tl_pm_or_executive_may_read_it(journey):
    with journey.engine.begin() as c:
        with pytest.raises(AuthorizationError):
            _require_report_role(c, tenant_id=journey.tenant_id, actor_id=journey.actor_id, functional_role="PC")
        c.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                 "VALUES (:t, 'exec-1', 'Executive')"),
            {"t": journey.tenant_id},
        )
        _require_report_role(c, tenant_id=journey.tenant_id, actor_id="exec-1", functional_role=None)


def test_tasks_on_a_journey_not_uploaded_that_day_are_reported_apart(journey):
    """2026-10-20: PCs who uploaded nothing showed 32, 111, 65 "tasks opened"
    -- tasks the system's own re-checks raised on their earlier journeys.
    Those are reported apart, never as the PC's work of the day."""
    tl = _tl(journey)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text("INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, "
                 "source_type, title, description, dedupe_key, assigned_role_code, task_status, completion_protocol) "
                 "VALUES (:t, :j, 'PC_UPLOAD_STATUS', 'DOCUMENT', 'SYSTEM', 'DOCUMENT', 'Check', 'x', :k, "
                 "'PC', 'READY', 'MACHINE_VERIFIED')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": f"k-{uuid4().hex}"},
        )

    report = _report(journey, tl)
    pc = report["pcs"][0]
    assert pc["journeysUploaded"] == 0
    assert pc["tasksOpened"] == 0
    assert pc["earlier"]["journeys"] == 1 and pc["earlier"]["tasksOpened"] == 1
    assert report["journeys"][0]["uploadedOnDay"] is False


def test_rejections_at_upload_are_counted_by_reason_and_by_day(journey):
    """2026-10-20: the daily report shows how many pages the scan-quality check
    rejected, why, and the last 14 days, to judge what is being uploaded."""
    tl = _tl(journey)
    _b1, faded, _ = add_page(journey, page_number=1, status="FAILED")
    _b2, blurred, _ = add_page(journey, page_number=2, status="FAILED")
    add_page(journey, page_number=3, status="READY")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        for queue_id, code in ((faded, "DI_QUALITY_IMAGE_CONTRAST"), (blurred, "DI_QUALITY_IMAGE_BLUR_SCORE")):
            c.execute(text("UPDATE auditcore.p2_document_queue SET last_error=:e WHERE queue_id=:q"),
                      {"e": code, "q": queue_id})

    report = _report(journey, tl)
    quality = report["quality"]
    assert quality["documents"] == 3 and quality["rejected"] == 2
    assert quality["rejectedPct"] == 66.7
    assert quality["byReason"]["faded"] == 1 and quality["byReason"]["blurred"] == 1
    assert report["pcs"][0]["rejections"]["faded"] == 1
    assert len(report["trend"]) == 14
    today = report["trend"][-1]
    assert today["day"] == report["day"] and today["rejected"] == 2 and today["documents"] == 3


def test_how_the_pages_became_documents_is_shown(journey):
    """2026-10-20: "48 pages but 41 documents, where are the other pages?"
    A multi-page document is one document; a repeated page is dropped."""
    tl = _tl(journey)
    _b1, grouped, _ = add_page(journey, page_number=1, status="READY")
    _b2, repeated, _ = add_page(journey, page_number=4, status="READY")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_document_queue SET unit_kind='GROUP', page_numbers=ARRAY[1,2,3] "
                       "WHERE queue_id=:q"), {"q": grouped})
        c.execute(text("UPDATE auditcore.p2_document_queue SET queue_status='CANCELLED' WHERE queue_id=:q"),
                  {"q": repeated})
    row = _report(journey, tl)["journeys"][0]
    assert row["pageMath"] == {"multiPageDocuments": 1, "pagesInMultiPage": 3, "repeatPages": 1}
    assert row["documents"]["extracted"] == 1
    pc = _report(journey, tl)["pcs"][0]
    assert pc["pageMath"]["pagesInMultiPage"] == 3


# Delivery files closed (product owner 2026-11-02): a delivery file is closed
# when its mandatory documents are in and the PC's document tasks are closed.

def _delivery_file(journey, *, missing=(), uploaded_days_ago: int = 2):
    """Make the journey a delivery file: one DELIVERY page, uploaded
    ``uploaded_days_ago`` days ago, and the stage engine's stored DELIVERY
    REQUIRED_DOCUMENTS gate with ``missing`` requirement keys."""
    _b, page, _ = add_page(journey, page_number=9, status="READY")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text("UPDATE auditcore.p2_document_queue SET business_stage='DELIVERY', "
                 "created_at_utc=now() - make_interval(days => :d) WHERE queue_id=:q"),
            {"q": page, "d": uploaded_days_ago},
        )
        c.execute(
            text("INSERT INTO auditcore.p2_stage_gate_state (tenant_id, journey_id, stage_code, gate_key, "
                 "gate_status, details) VALUES (:t, :j, 'DELIVERY', 'REQUIRED_DOCUMENTS', :s, CAST(:d AS jsonb))"),
            {"t": journey.tenant_id, "j": journey.journey_id, "s": "WAITING" if missing else "PASS",
             "d": json.dumps({"missing": [{"key": k, "label": k} for k in missing]})},
        )


def _doc_task(journey, *, task_type="PC_PAGE_QUALITY_REJECTED", status="READY", closed_days_ago=None,
              requirement_key=None):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(
            text("INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, "
                 "source_type, source_code, title, description, dedupe_key, assigned_role_code, task_status, "
                 "completion_protocol, reference, verified_at_utc) "
                 "VALUES (:t, :j, :type, 'DOCUMENT_REUPLOAD', 'SYSTEM', 'DOCUMENT', :type, 'x', 'x', :k, 'PC', "
                 ":s, 'MACHINE_VERIFIED', CAST(:ref AS jsonb), "
                 "CASE WHEN CAST(:ago AS int) IS NULL THEN NULL ELSE now() - make_interval(days => CAST(:ago AS int)) END) "
                 "RETURNING task_id"),
            {"t": journey.tenant_id, "j": journey.journey_id, "type": task_type, "k": f"k-{uuid4().hex}",
             "s": status, "ago": closed_days_ago,
             "ref": json.dumps({"requirementKey": requirement_key} if requirement_key else {})},
        ).scalar_one()


def _report_on(journey, tl, days_ago: int):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return build_daily_processing_report(c, tenant_id=journey.tenant_id, actor_id=tl,
                                             day=_today(journey) - timedelta(days=days_ago))


def test_a_delivery_file_closes_on_the_day_its_last_document_task_closes(journey):
    tl = _tl(journey)
    _delivery_file(journey, missing=("rsa_invoice",), uploaded_days_ago=3)
    _doc_task(journey, status="VERIFIED_COMPLETE", closed_days_ago=2)
    # The PC closed "Upload the RSA Invoice" ("Document not required to be uploaded").
    _doc_task(journey, task_type="DOCUMENT_MISSING", status="VERIFIED_COMPLETE", closed_days_ago=1,
              requirement_key="rsa_invoice")

    closed = _report_on(journey, tl, 1)["deliveryFilesClosed"]
    assert closed["total"] == 1
    assert closed["files"][0]["journeyId"] == str(journey.journey_id)
    assert closed["files"][0]["customerName"] == "P2 Customer" and closed["files"][0]["outletName"] == "O"
    for earlier in (2, 3):
        assert _report_on(journey, tl, earlier)["deliveryFilesClosed"]["total"] == 0
    assert _report_on(journey, tl, 0)["deliveryFilesClosed"]["total"] == 0


def test_a_delivery_file_is_not_closed_while_a_document_task_is_open(journey):
    tl = _tl(journey)
    _delivery_file(journey, uploaded_days_ago=1)
    _doc_task(journey, status="VERIFIED_COMPLETE", closed_days_ago=1)
    open_task = _doc_task(journey, task_type="DOCUMENT_RESUBMIT", status="RETURNED")
    assert _report_on(journey, tl, 1)["deliveryFilesClosed"]["total"] == 0

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_tasks SET task_status='VERIFIED_COMPLETE', verified_at_utc=now() "
                       "WHERE task_id=:id"), {"id": open_task})
    assert _report_on(journey, tl, 0)["deliveryFilesClosed"]["total"] == 1
    assert _report_on(journey, tl, 1)["deliveryFilesClosed"]["total"] == 0


def test_a_missing_mandatory_document_keeps_the_file_open(journey):
    """The tax invoice is not in and nothing closed its ask: not closed,
    even with no open task."""
    tl = _tl(journey)
    _delivery_file(journey, missing=("tax_invoice_tally",), uploaded_days_ago=0)
    assert _report_on(journey, tl, 0)["deliveryFilesClosed"]["total"] == 0


def test_a_delivery_file_with_no_document_task_closes_on_its_last_delivery_upload(journey):
    tl = _tl(journey)
    _delivery_file(journey, uploaded_days_ago=1)
    assert _report_on(journey, tl, 1)["deliveryFilesClosed"]["total"] == 1
    assert _report_on(journey, tl, 0)["deliveryFilesClosed"]["total"] == 0


def test_a_booking_only_journey_never_counts(journey):
    tl = _tl(journey)
    add_page(journey, page_number=1, status="READY")  # a booking page, no delivery
    _doc_task(journey, status="VERIFIED_COMPLETE", closed_days_ago=0)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text("INSERT INTO auditcore.p2_stage_gate_state (tenant_id, journey_id, stage_code, gate_key, "
                 "gate_status, details) VALUES (:t, :j, 'DELIVERY', 'REQUIRED_DOCUMENTS', 'PASS', '{}')"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
    assert _report_on(journey, tl, 0)["deliveryFilesClosed"]["total"] == 0


def test_a_closed_delivery_file_counts_under_the_journeys_own_pc(journey):
    """The journey's PC (who started it), not whoever uploaded or closed."""
    tl = _tl(journey)
    _delivery_file(journey, uploaded_days_ago=0)
    # Someone else uploaded today; the journey's PC uploaded nothing today.
    other = f"pc-other-{uuid4().hex[:6]}"
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_upload_batches SET uploaded_by_actor_id=:a "
                       "WHERE tenant_id=:t AND journey_id=:j"),
                  {"a": other, "t": journey.tenant_id, "j": journey.journey_id})
    report = _report_on(journey, tl, 0)
    closed = report["deliveryFilesClosed"]
    assert [(p["pcActorId"], p["pcName"], p["deliveryFilesClosed"]) for p in closed["byPc"]] == [
        (journey.actor_id, "Subhakanta Jena", 1)]
    by_pc = {p["pcActorId"]: p for p in report["pcs"]}
    assert by_pc[journey.actor_id]["deliveryFilesClosed"] == 1
    assert by_pc[journey.actor_id]["journeysUploaded"] == 0
    assert by_pc[other]["deliveryFilesClosed"] == 0 and by_pc[other]["journeysUploaded"] == 1


# Exact duplicate files (product owner 2026-11-03): every page uploaded before, nothing new.
def _file_with_pages(journey, shas: list[str], *, minutes_ago: float) -> str:
    """One uploaded file whose pages carry these fingerprints, uploaded ``minutes_ago``."""
    batch = None
    for n, sha in enumerate(shas, start=1):
        b, queue, _ = add_page(journey, page_number=n, status="READY")
        with journey.engine.begin() as c:
            set_tenant_context(c, journey.tenant_id)
            if batch is None:
                batch = b
            else:  # the same file: every page in the first upload
                c.execute(text("UPDATE auditcore.p2_document_queue SET batch_id=:b WHERE tenant_id=:t AND queue_id=:q"),
                          {"b": batch, "t": journey.tenant_id, "q": queue})
                c.execute(text("DELETE FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND batch_id=:b"),
                          {"t": journey.tenant_id, "b": b})
            c.execute(text("UPDATE auditcore.p2_document_queue SET page_sha256=:s, unit_kind='PAGE' "
                           "WHERE tenant_id=:t AND queue_id=:q"), {"s": sha, "t": journey.tenant_id, "q": queue})
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_upload_batches SET created_at_utc = now() - (:m * interval '1 minute'), "
                       "page_count=:n WHERE tenant_id=:t AND batch_id=:b"),
                  {"m": minutes_ago, "n": len(shas), "t": journey.tenant_id, "b": batch})
    return str(batch)


def test_exact_duplicate_files_are_counted_per_pc(journey):
    tl = _tl(journey)
    a, b, c = (uuid4().hex for _ in range(3))
    # Minutes apart, not half an hour: run just after midnight IST the first file
    # would otherwise fall on the day before the report's (CI, 5 Nov 00:15 IST).
    first = _file_with_pages(journey, [a, b], minutes_ago=3)           # new
    again = _file_with_pages(journey, [b, a], minutes_ago=2)           # all pages seen before
    more = _file_with_pages(journey, [a, c], minutes_ago=1)            # one new page: not a duplicate
    with journey.engine.begin() as conn:                                # the same file uploaded again, cancelled
        set_tenant_context(conn, journey.tenant_id)
        conn.execute(
            text("INSERT INTO auditcore.p2_upload_batches (tenant_id, batch_id, journey_id, original_filename, "
                 "content_type, size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id, "
                 "sha256) VALUES (:t, :id, :j, 'scan.pdf', 'application/pdf', 100, 0, 'k', 'CANCELLED', :a, :sha)"),
            {"t": journey.tenant_id, "id": uuid4(), "j": journey.journey_id, "a": journey.actor_id,
             "sha": uuid4().hex})
    report = _report(journey, tl)
    flags = {f["batchId"]: f["exactDuplicate"] for f in report["files"]}
    assert (flags[first], flags[again], flags[more]) == (False, True, False)
    [pc] = report["documentStatus"]["byPc"]
    assert pc["exactDuplicateFiles"] == 2 and report["documentStatus"]["total"]["exactDuplicateFiles"] == 2
