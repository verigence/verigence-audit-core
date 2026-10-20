"""Daily processing report (2026-10-20): per PC, per day, from stored data only."""
from __future__ import annotations

from datetime import UTC, datetime
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
