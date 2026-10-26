"""Daily Processing additions of 2026-10-26 (real Postgres):

- a file of more than 25 pages gets the PC a notice to review it; nothing is
  blocked (the delivery gate never sees it), the PC closes it with a comment,
  and the report highlights the file with what the PC answered;
- every file uploaded on the day is listed with its state and a link that
  opens the original (a file that never arrived has none);
- document status per PC: uploaded, not uploaded, rejected, retried, waiting;
- work done: what each PC / Team Lead did on tasks (the system's own events
  are not work);
- the app endpoint that issues a fresh link to an uploaded file.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_batch_pages,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_daily_report import build_daily_processing_report
from audit_core.uc03_p2_runtime import LARGE_UPLOAD_PAGES
from audit_core.uc03_p2_stage import _open_tasks_for
from audit_core.uc03_p2_task_producer import (
    FILE_NOT_UPLOADED_IN_TIME_REASON,
    LARGE_UPLOAD_TASK_TYPE,
    sync_large_upload_tasks,
)
from audit_core.uc03_p2_tasks import record_task_event


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2large")
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


class FakeStorage:
    def presign_get(self, object_key, *, expires_seconds=1800):
        return f"https://signed.example/{object_key}?expires={expires_seconds}"


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    import audit_core.uc03_p2_api as api
    import audit_core.uc03_p2_storage as storage_module

    monkeypatch.setattr(storage_module, "get_p2_document_storage", lambda: FakeStorage())
    monkeypatch.setattr(api, "get_p2_document_storage", lambda: FakeStorage())


def _sql(journey, statement, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        result = connection.execute(text(statement), {"t": journey.tenant_id, "j": journey.journey_id, **params})
        return result.mappings().all() if result.returns_rows else None


def _file(journey, pages: int, status="READY"):
    return add_batch_pages(journey, [(None, status)] * pages)[0]


def _sync(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return sync_large_upload_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _notice(journey, batch_id):
    rows = _sql(journey, "SELECT task_id, task_type, task_status, severity, assigned_role_code, category, title, "
                         "description, reference, allowed_actions FROM auditcore.p2_tasks "
                         "WHERE tenant_id=:t AND dedupe_key=:k", k=f"large-upload:{journey.journey_id}:{batch_id}")
    return rows[0] if rows else None


def _tl(journey) -> str:
    tl = f"tl-{uuid4().hex[:8]}"
    with journey.engine.begin() as connection:
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                 "VALUES (:t, :a, 'TL')"),
            {"t": journey.tenant_id, "a": tl},
        )
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.journeys SET created_by_actor_id=:a, created_by_display_name='Subhakanta Jena' "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"a": journey.actor_id, "t": journey.tenant_id, "j": journey.journey_id},
        )
    return tl


def _report(journey, tl):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        day = connection.execute(text("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date")).scalar_one()
        return build_daily_processing_report(connection, tenant_id=journey.tenant_id, actor_id=tl, day=day)


def _act(journey, task_id, action, comment=None, subject=None):
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=subject or journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    body = {"action": action, **({"comment": comment} if comment is not None else {})}
    return TestClient(app, raise_server_exceptions=False).post(
        f"/p2/v1/tenants/{journey.tenant_id}/tasks/{task_id}/actions", json=body)


def _gate(journey) -> list[str]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [t["taskType"] for t in _open_tasks_for(connection, tenant_id=journey.tenant_id,
                                                        journey_id=journey.journey_id, role="PC",
                                                        tabs=("DOCUMENTS", "MANUAL_VERIFICATION"))]


# ------------------------------------------------------------------ the large-upload notice

def test_the_limit_is_25_pages_and_more_than_25_is_large():
    assert LARGE_UPLOAD_PAGES == 25


def test_a_file_of_more_than_25_pages_gets_the_pc_an_info_notice_and_blocks_nothing(journey):
    exactly = _file(journey, 25)
    large = _file(journey, 27)

    assert _sync(journey) == {"RAISED": 1}

    assert _notice(journey, exactly) is None
    notice = _notice(journey, large)
    assert notice["task_type"] == LARGE_UPLOAD_TASK_TYPE and notice["task_status"] == "READY"
    assert notice["severity"] == "INFO" and notice["assigned_role_code"] == "PC"
    assert "27 pages" in notice["title"] and "really needed" in notice["description"]
    assert "Nothing is blocked" in notice["description"]
    assert notice["reference"]["pageCount"] == 27 and notice["reference"]["threshold"] == 25
    assert notice["allowed_actions"] == ["CLOSE_TASK", "ADD_COMMENT"]
    # The delivery gate looks at the Documents and Manual Verification tabs: not this.
    assert _gate(journey) == []
    # Idempotent.
    assert _sync(journey) == {"UNCHANGED": 1}


def test_the_pc_closes_the_notice_with_a_comment_and_it_does_not_come_back(journey):
    batch_id = _file(journey, 30)
    _sync(journey)
    task_id = _notice(journey, batch_id)["task_id"]

    assert _act(journey, task_id, "CLOSE_TASK").status_code in (400, 422)  # a comment is required
    response = _act(journey, task_id, "CLOSE_TASK", "All 30 pages are the customer's KYC set")
    assert response.status_code == 200, response.text

    assert _notice(journey, batch_id)["task_status"] == "VERIFIED_COMPLETE"
    assert _sync(journey) == {"ACCEPTED_EXCEPTION": 1}
    assert _notice(journey, batch_id)["task_status"] == "VERIFIED_COMPLETE"


def test_the_notice_closes_itself_when_the_file_goes_away(journey):
    batch_id = _file(journey, 40)
    _sync(journey)
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET batch_status='CANCELLED' WHERE batch_id=:b", b=batch_id)

    assert _sync(journey) == {"VERIFIED": 1}
    assert _notice(journey, batch_id)["task_status"] == "VERIFIED_COMPLETE"


# ----------------------------------------------------------- the report: files, status, links

def test_the_report_lists_every_file_with_a_link_and_highlights_the_large_one(journey):
    tl = _tl(journey)
    small = _file(journey, 3)
    large = _file(journey, 28)
    _sync(journey)
    _act(journey, _notice(journey, large)["task_id"], "ADD_COMMENT", "Checking with the customer")

    report = _report(journey, tl)

    by_batch = {f["batchId"]: f for f in report["files"]}
    assert by_batch[str(small)]["large"] is False and by_batch[str(small)]["review"] is None
    big = by_batch[str(large)]
    assert big["large"] is True and big["pages"] == 28 and big["state"] == "READ"
    assert big["review"] == {"status": "OPEN", "comment": "Checking with the customer"}
    assert big["pcName"] == "Subhakanta Jena"
    assert big["openUrl"] == f"https://signed.example/p2/{large}?expires=1800"
    assert big["filePath"] == f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/uploads/{large}/file"
    assert report["largeUploads"]["threshold"] == 25 and report["largeUploads"]["count"] == 1
    assert [f["batchId"] for f in report["largeUploads"]["files"]] == [str(large)]
    assert report["documentStatus"]["total"]["largeUploads"] == 1

    task_id = _notice(journey, large)["task_id"]
    _act(journey, task_id, "CLOSE_TASK", "Needed: full KYC set")
    closed = {f["batchId"]: f for f in _report(journey, tl)["files"]}[str(large)]
    assert closed["review"] == {"status": "CLOSED", "comment": "Needed: full KYC set"}


def test_a_file_that_never_arrived_is_counted_as_not_uploaded_and_has_no_link(journey):
    tl = _tl(journey)
    read = _file(journey, 2)
    never = _file(journey, 1)
    pending = _file(journey, 1)
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET batch_status='FAILED', page_count=0, failure_reason=:r, "
                  "original_filename='lost.pdf' WHERE batch_id=:b", b=never, r=FILE_NOT_UPLOADED_IN_TIME_REASON)
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET batch_status='AWAITING_UPLOAD', page_count=0 "
                  "WHERE batch_id=:b", b=pending)
    _sql(journey, "DELETE FROM auditcore.p2_document_queue WHERE batch_id = ANY(:b)", b=[never, pending])

    report = _report(journey, tl)

    by_batch = {f["batchId"]: f for f in report["files"]}
    assert by_batch[str(read)]["state"] == "READ" and by_batch[str(read)]["openUrl"]
    lost = by_batch[str(never)]
    assert lost["state"] == "NOT_UPLOADED" and lost["openUrl"] is None and lost["filePath"] is None
    assert lost["failureReason"] == FILE_NOT_UPLOADED_IN_TIME_REASON
    assert by_batch[str(pending)]["state"] == "UPLOAD_PENDING" and by_batch[str(pending)]["openUrl"] is None
    total = report["documentStatus"]["total"]
    assert total["filesUploaded"] == 1 and total["filesNotUploaded"] == 1 and total["filesUploadPending"] == 1
    [pc] = [p for p in report["documentStatus"]["byPc"] if p["pcActorId"] == journey.actor_id]
    assert pc["filesNotUploaded"] == 1 and pc["pcName"] == "Subhakanta Jena"


def test_document_status_counts_rejected_and_retried_in_the_product_owners_words(journey):
    tl = _tl(journey)
    _batch_id, rows = add_batch_pages(journey, [(None, "READY"), (None, "FAILED"), (None, "FAILED"), (None, "QUEUED")])
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='DI_QUALITY_IMAGE_BLUR_SCORE' "
                  "WHERE queue_id=:q", q=rows[1]["queue_id"])
    _sql(journey, "UPDATE auditcore.p2_document_queue SET last_error='DOCUMENT_AI_UNAVAILABLE' "
                  "WHERE queue_id=:q", q=rows[2]["queue_id"])
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        from audit_core.uc03_p2_runtime import record_activity

        for event_type, details in (("PAGE_RETRY_REQUESTED", {"by": journey.actor_id}),
                                    ("PAGE_REREAD_REQUESTED", {"by": journey.actor_id}),
                                    ("DOCUMENT_STATUS_CHANGED", {"status": "RETRY_WAIT"}),
                                    ("P2_WORK_DEAD_LETTER", {"workType": "DOCUMENT_INGEST"})):
            record_activity(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                            event_type=event_type, subject_type="DOCUMENT_PAGE", subject_id=str(rows[0]["queue_id"]),
                            details=details)

    total = _report(journey, tl)["documentStatus"]["total"]

    assert total["filesUploaded"] == 1 and total["documents"] == 4
    assert total["read"] == 1 and total["rejected"] == 1 and total["failedOther"] == 1 and total["waiting"] == 1
    assert total["retriedAutomatically"] == 1 and total["retriedByPc"] == 2 and total["retriesExhausted"] == 1


# ------------------------------------------------------------------------------- work done

def _task_row(journey, title="Check"):
    return _sql(journey, "INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, "
                         "source_type, title, description, dedupe_key, assigned_role_code, task_status, "
                         "completion_protocol) VALUES (:t, :j, 'DOCUMENT_MISSING', 'DOCUMENT_EXCEPTION', 'SYSTEM', "
                         "'DOCUMENT', :title, 'x', :k, 'PC', 'READY', 'MACHINE_VERIFIED') RETURNING task_id",
                title=title, k=f"k-{uuid4().hex}")[0]["task_id"]


def test_work_done_counts_what_each_person_did_and_ignores_the_system(journey):
    tl = _tl(journey)
    first, second = _task_row(journey, "First"), _task_row(journey, "Second")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)

        def event(task, kind, actor, role, comment=None):
            record_task_event(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, task_id=task,
                              event_type=kind, actor_id=actor, actor_role_code=role, comment=comment)

        event(first, "RAISED", "SYSTEM", "SYSTEM")
        event(first, "ADD_COMMENT", journey.actor_id, "PC", "On it")
        event(first, "UPLOAD_DOCUMENT", journey.actor_id, "PC")
        event(second, "UPLOAD_DOCUMENT", journey.actor_id, "PC")
        event(first, "MACHINE_VERIFICATION_PASS", "SYSTEM", "SYSTEM")
        event(second, "ACCEPT_EXCEPTION", tl, "TL", "Fine")
        connection.execute(
            text("UPDATE auditcore.p2_tasks SET task_status='VERIFIED_COMPLETE', verified_at_utc=now() "
                 "WHERE task_id=:i"), {"i": second})

    work = _report(journey, tl)["teamWork"]

    people = {(p["actorId"], p["role"]): p for p in work["people"]}
    pc = people[(journey.actor_id, "PC")]
    assert pc["name"] == "Subhakanta Jena"
    assert pc["tasksWorked"] == 2 and pc["actions"] == 3 and pc["tasksClosed"] == 1
    assert pc["byAction"] == {"ADD_COMMENT": 1, "UPLOAD_DOCUMENT": 2}
    assert pc["byTaskType"] == {"DOCUMENT_MISSING": 2} and pc["journeys"] == 1
    first_task = next(t for t in pc["tasks"] if t["title"] == "First")
    assert first_task["actions"] == ["ADD_COMMENT", "UPLOAD_DOCUMENT"] and first_task["lastComment"] == "On it"
    lead = people[(tl, "TL")]
    assert lead["tasksWorked"] == 1 and lead["tasksClosed"] == 1 and lead["name"] is None
    assert not [p for p in work["people"] if p["actorId"].upper() == "SYSTEM"]
    assert work["totals"] == {"people": 2, "tasksWorked": 3, "actions": 4, "tasksClosed": 2}


# ------------------------------------------------------------------------- fresh file link

def _api(journey, path, subject=None):
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=subject or journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    return TestClient(app, raise_server_exceptions=False).get(f"/p2/v1/tenants/{journey.tenant_id}{path}")


def test_the_upload_link_issues_a_short_lived_link_to_the_original(journey):
    batch_id = _file(journey, 2)
    response = _api(journey, f"/journeys/{journey.journey_id}/uploads/{batch_id}/file")
    assert response.status_code == 200, response.text
    assert response.json() == {"batchId": str(batch_id), "filename": "packet.pdf", "pages": 2,
                               "status": "PROCESSING", "url": f"https://signed.example/p2/{batch_id}?expires=1800",
                               "expiresInSeconds": 1800}
    assert _api(journey, f"/journeys/{journey.journey_id}/uploads/{uuid4()}/file").status_code == 404
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET batch_status='AWAITING_UPLOAD' WHERE batch_id=:b", b=batch_id)
    assert _api(journey, f"/journeys/{journey.journey_id}/uploads/{batch_id}/file").status_code == 404
