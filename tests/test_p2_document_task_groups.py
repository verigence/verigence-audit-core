"""The Documents group of the Task Queue (product owner 2026-10-21):
manual verification, failed to read, image quality and missing documents,
with one image-quality task per file for pages the scan-quality check
refused."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from audit_core.uc03_p2_document_task_groups import (
    DOCUMENT_CATEGORY_BY_TASK_TYPE,
    count_by_category,
    document_category,
    document_detail,
)
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_task_producer import sync_processing_failure_tasks
from audit_core.uc03_p2_tasks import create_p2_task, task_queue_tab

# ---------------------------------------------------------------- the map


@pytest.mark.parametrize(
    ("task_type", "category", "source_code", "expected"),
    [
        ("MANUAL_VERIFICATION_REVIEW", "DOCUMENT_VERIFICATION", "MANUAL_VERIFICATION", "MANUAL_VERIFICATION"),
        ("PC_BOOKING_DATE_MISSING", "DOCUMENT_VERIFICATION", "BOOKING_DATE_MISSING", "MANUAL_VERIFICATION"),
        ("FIELD_CORRECTION_REVIEW_P2", "CORRECTION_APPROVAL", None, "MANUAL_VERIFICATION"),
        ("DUPLICATE_RECEIPT_NOTICE", "DUPLICATE_RECEIPT_NOTICE", None, "MANUAL_VERIFICATION"),
        ("DOCUMENT_MISSING", "DOCUMENT_REUPLOAD", "WRONG_CUSTOMER_NAME", "MANUAL_VERIFICATION"),
        ("PC_UPLOAD_STATUS", "DOCUMENT_EXCEPTION", "DOCUMENT_PROCESSING_FAILED", "FAILED_TO_READ"),
        ("PC_VERIFY_UNRECOGNIZED_DOCUMENT", "DOCUMENT_VERIFICATION", "DOCUMENT_UNCLASSIFIED", "FAILED_TO_READ"),
        ("TL_DOCUMENT_STUCK", "SUPERVISORY_NOTICE", "DOCUMENT_PROCESSING_STUCK", "FAILED_TO_READ"),
        ("PC_RESOLVE_DOCUMENT_PROCESSING_FAILURE", "PC_RESOLVE_DOCUMENT_PROCESSING_FAILURE", None, "FAILED_TO_READ"),
        ("PC_PAGE_QUALITY_REJECTED", "DOCUMENT_REUPLOAD", "DOCUMENT_QUALITY_REJECTED", "IMAGE_QUALITY"),
        ("DOCUMENT_MISSING", "DOCUMENT_REUPLOAD", "DOCUMENT_MISSING", "MISSING_DOCUMENT"),
        ("TL_DOCUMENT_UPLOAD", "DOCUMENT_REUPLOAD", None, "MISSING_DOCUMENT"),
        ("PC_DOCUMENT_REUPLOAD", "PC_DOCUMENT_REUPLOAD", None, "MISSING_DOCUMENT"),
        # A rule-raised task: the rule decides.
        ("DOCUMENT_REMEDIATION", "DOCUMENT_REUPLOAD", "BK_DOCKET_PRESENT", "MISSING_DOCUMENT"),
        ("DOCUMENT_REMEDIATION", "DOCUMENT_REUPLOAD", "DL_V2_DOCUMENT_PROCESSING_FAILED", "FAILED_TO_READ"),
        ("DOCUMENT_REMEDIATION", "DOCUMENT_REUPLOAD", "UC03_DI_LOW_CONFIDENCE_POST_SUBMIT", "MANUAL_VERIFICATION"),
        ("FINDING_REMEDIATION", "FINDING_REMEDIATION", "DOCUMENT_UNRECOGNIZED", "FAILED_TO_READ"),
        ("FINDING_REMEDIATION", "FINDING_REMEDIATION", "DOCUMENT_MISSING", "MISSING_DOCUMENT"),
        # Not document work: stays where it was.
        ("FINDING_REMEDIATION", "FINDING_REMEDIATION", "MODEL_NOT_IDENTIFIED", None),
        ("SKU_CLOSEST_MATCH_CONFIRM", "CORRECTION_APPROVAL", None, None),
        ("PC_CONFIRMATION", "PROCESS_CONFIRMATION", "CASH_INTIMATION_UNCONFIRMED", None),
        ("PC_CONFIRMATION", "PROCESS_CONFIRMATION", "INSURANCE_INVOICE_MISSING", "MISSING_DOCUMENT"),
        # External rule engine checks for a document not on file.
        *[("RULE_DISCREPANCY_REVIEW", "CROSS_DOCUMENT_REVIEW", code, "MISSING_DOCUMENT") for code in (
            "INVOICE_MISSING", "GATE_PASS_MISSING", "RTO_CHALLAN_MISSING", "INSURANCE_COVER_NOTE_MISSING",
            "DELIVERY_ORDER_MISSING", "CORPORATE_PO_MISSING", "CUSTOMER_LEDGER_MISSING")],
        ("RULE_DISCREPANCY_REVIEW", "CROSS_DOCUMENT_REVIEW", "BK_PRICE_MATCH", None),
        ("FINDING_REVIEW", "FINDING_REMEDIATION", "ANY_RULE", None),
        ("MODEL_SELECTION_CORRECTION_REVIEW", "CORRECTION_APPROVAL", None, None),
        ("TL_MANAGEMENT_REFERRAL", "PROCESS_CONFIRMATION", None, None),
        ("DELIVERY_REVIEW", "TASK_CONFIRMATION", "DELIVERY_REVIEW", None),
    ],
)
def test_every_task_type_has_its_document_category(task_type, category, source_code, expected):
    assert document_category(task_type, category, source_code) == expected


def test_a_new_resubmit_category_slots_in_by_its_key():
    # DOCUMENT_RESUBMIT (being added on another branch) under any task type name.
    assert document_category("PC_DOCUMENT_RESUBMIT", "DOCUMENT_RESUBMIT", None) == "FAILED_TO_READ"
    assert document_category("SOMETHING_NEW", "SOMETHING_NEW", None) is None


def test_every_mapped_p2_task_type_exists_or_is_a_known_legacy_type():
    registry = get_registry()
    legacy = {"FIELD_CORRECTION_REVIEW", "PC_RESOLVE_DOCUMENT_PROCESSING_FAILURE", "PC_DOCUMENT_REUPLOAD",
              "DOCUMENT_RESUBMIT"}
    assert set(DOCUMENT_CATEGORY_BY_TASK_TYPE) - set(registry.tasks) <= legacy


def test_the_delivery_gate_tabs_are_unchanged_for_document_work():
    # The delivery gate (PC_TASKS_CLOSED) still reads task_queue_tab; the
    # image-quality task blocks it like the file's status task does.
    assert task_queue_tab("PC_PAGE_QUALITY_REJECTED", "DOCUMENT_REUPLOAD") == "DOCUMENTS"
    assert task_queue_tab("MANUAL_VERIFICATION_REVIEW", "DOCUMENT_VERIFICATION") == "MANUAL_VERIFICATION"
    assert task_queue_tab("DUPLICATE_RECEIPT_NOTICE", "DOCUMENT_EXCEPTION") == "DOCUMENTS"


def test_rows_say_file_pages_and_reason_in_plain_words():
    quality = document_detail({
        "task_type": "PC_PAGE_QUALITY_REJECTED", "source_code": "DOCUMENT_QUALITY_REJECTED",
        "reference": {"filename": "bundle.pdf", "pageNumbers": [3, 5, 7],
                      "rejections": [{"reason": "faded", "pages": [3]}, {"reason": "blurred", "pages": [5, 7]}]},
    }, "IMAGE_QUALITY")
    assert quality == {
        "category": "IMAGE_QUALITY", "categoryLabel": "Image quality", "file": "bundle.pdf", "pages": [3, 5, 7],
        "reason": "Page 3 is too faded (low contrast); pages 5 and 7 are blurred. Re-scan and upload again.",
        "documentId": None,
    }
    failed = document_detail({
        "task_type": "PC_UPLOAD_STATUS", "source_code": "DOCUMENT_PROCESSING_FAILED",
        "reference": {"filename": "bundle.pdf", "pageNumbers": [12], "retrying": False},
    }, "FAILED_TO_READ")
    assert (failed["file"], failed["pages"], failed["reason"]) == ("bundle.pdf", [12], "Page 12 could not be processed.")
    slow = document_detail({
        "task_type": "PC_UPLOAD_STATUS", "source_code": "DOCUMENT_PROCESSING_SLOW",
        "reference": {"filename": "a.pdf", "pages": {"total": 4, "read": 1}, "retrying": True},
    }, "FAILED_TO_READ")
    assert slow["reason"] == "Not read yet: 1 of 4 pages read so far. Nothing to do yet."
    document_id = str(uuid4())
    review = document_detail({
        "task_type": "MANUAL_VERIFICATION_REVIEW", "source_code": "MANUAL_VERIFICATION",
        "reference": {"documentId": document_id, "templateKey": "booking_docket",
                      "fields": [{"fieldKey": "ex_showroom_price", "reasons": ["LOW_CONFIDENCE"], "value": None},
                                 {"fieldKey": "booking_date", "reasons": ["LOW_CONFIDENCE"], "value": None}]},
    }, "MANUAL_VERIFICATION", get_registry())
    assert review["file"] == "Booking Docket" and review["documentId"] == document_id
    assert review["reason"] == "Check 2 values: Ex showroom price, Booking date. Read with low confidence."
    missing = document_detail({
        "task_type": "DOCUMENT_MISSING", "source_code": "DOCUMENT_MISSING", "title": "Upload the Corporate ID",
        "description": "A corporate discount is on the booking form. The Corporate ID is therefore mandatory.",
        "reference": {},
    }, "MISSING_DOCUMENT")
    assert missing["file"] == "Corporate ID"
    assert missing["reason"] == "Not on file yet. A corporate discount is on the booking form."


def test_counts_per_category_across_the_queue_and_per_journey():
    a, b = str(uuid4()), str(uuid4())
    totals, by_journey = count_by_category([
        {"journey_id": a, "document_category": "IMAGE_QUALITY"},
        {"journey_id": a, "document_category": "MANUAL_VERIFICATION"},
        {"journey_id": b, "document_category": "IMAGE_QUALITY"},
        {"journey_id": b, "document_category": None},
    ])
    assert totals == {"TOTAL": 3, "MANUAL_VERIFICATION": 1, "FAILED_TO_READ": 0, "IMAGE_QUALITY": 2,
                      "MISSING_DOCUMENT": 0}
    assert by_journey[a]["TOTAL"] == 2 and by_journey[b] == {
        "TOTAL": 1, "MANUAL_VERIFICATION": 0, "FAILED_TO_READ": 0, "IMAGE_QUALITY": 1, "MISSING_DOCUMENT": 0}


# ------------------------------------------------- the image-quality task


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2dtg")
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _fail(journey, queue_id, code: str, *, minutes_ago: float = 0.0) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET queue_status='FAILED', last_error=:e, "
                 "updated_at_utc=now() - (:ago * interval '1 minute') WHERE queue_id=:q"),
            {"e": code, "q": queue_id, "ago": minutes_ago},
        )


def _sync(journey) -> dict:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return sync_processing_failure_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _task(journey, key: str) -> dict | None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        row = connection.execute(
            text("SELECT task_type, title, description, reference, task_status, severity, assigned_role_code "
                 "FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k"),
            {"t": journey.tenant_id, "k": key},
        ).mappings().one_or_none()
    return dict(row) if row else None


def _unrelated_upload(journey):
    """Another file on the booking (an insurance cover note), fully read."""
    batch, _ = add_batch_pages(journey, [("insurance_cover_note", "READY")])
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_upload_batches SET batch_status='COMPLETED' WHERE batch_id=:b"),
                           {"b": batch})
    return batch


def _gate_tasks(journey) -> list[str]:
    """What the delivery gate PC_TASKS_CLOSED sees as open PC work."""
    from audit_core.uc03_p2_stage import _open_tasks_for

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [t["taskType"] for t in _open_tasks_for(connection, tenant_id=journey.tenant_id,
                                                        journey_id=journey.journey_id, role="PC",
                                                        tabs=("DOCUMENTS", "MANUAL_VERIFICATION"))]


def _page_status(journey, queue_id, status: str) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_document_queue SET queue_status=:s WHERE queue_id=:q"),
                           {"s": status, "q": queue_id})


def _batch_status(journey, batch, status: str) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_upload_batches SET batch_status=:s WHERE batch_id=:b"),
                           {"s": status, "b": batch})


def test_refused_pages_get_their_own_task_per_file(journey):
    batch, pages = add_batch_pages(journey, [("booking_form", "READY")] + [(None, "QUEUED")] * 4)
    _fail(journey, pages[1]["queue_id"], "DI_QUALITY_IMAGE_CONTRAST")   # page 2
    _fail(journey, pages[2]["queue_id"], "DI_QUALITY_IMAGE_BLUR_SCORE")  # page 3
    _fail(journey, pages[3]["queue_id"], "DI_QUALITY_IMAGE_MIN_DIMENSIONS")  # page 4
    _fail(journey, pages[4]["queue_id"], "DOCUMENT_AI_RESPONSE_INVALID")  # page 5
    _batch_status(journey, batch, "PARTIAL_FAILURE")
    _sync(journey)

    quality = _task(journey, f"upload-quality:{journey.journey_id}:{batch}")
    assert quality["task_type"] == "PC_PAGE_QUALITY_REJECTED" and quality["assigned_role_code"] == "PC"
    assert quality["severity"] == "HIGH" and quality["task_status"] == "READY"
    assert quality["title"] == "packet.pdf: 3 pages refused for image quality"
    assert quality["description"].startswith(
        "Refused by the scan-quality check and not read: Page 2 is too faded (low contrast); Page 3 is blurred; "
        "Page 4 is too small. A retry will not help.")
    assert quality["reference"]["pageNumbers"] == [2, 3, 4]
    assert [r["reason"] for r in quality["reference"]["rejections"]] == ["faded", "blurred", "tooSmall"]

    # The file's status task now speaks only of the page that failed otherwise.
    status = _task(journey, f"upload-status:{journey.journey_id}:{batch}")
    assert status["title"] == "packet.pdf: 1 page could not be processed"
    assert status["reference"]["pageNumbers"] == [5]
    assert "faded" not in status["description"] and "blurred" not in status["description"]
    assert sorted(_gate_tasks(journey)) == ["PC_PAGE_QUALITY_REJECTED", "PC_UPLOAD_STATUS"]

    # One refused page is read after all: the task names only the others.
    _page_status(journey, pages[1]["queue_id"], "READY")
    _sync(journey)
    assert _task(journey, f"upload-quality:{journey.journey_id}:{batch}")["reference"]["pageNumbers"] == [3, 4]


def test_an_unrelated_upload_never_closes_it_and_the_gate_stays_blocked(journey):
    """Review of #479: bundle page 1 read as the booking form, page 2
    refused for blur; the PC then uploads an insurance cover note. The
    quality task stays open and the delivery gate stays blocked, as the
    status task kept it on origin/dev."""
    batch, pages = add_batch_pages(journey, [("booking_form", "READY"), (None, "QUEUED")])
    _fail(journey, pages[1]["queue_id"], "DI_QUALITY_IMAGE_BLUR_SCORE", minutes_ago=30)
    _batch_status(journey, batch, "PARTIAL_FAILURE")
    _sync(journey)
    _unrelated_upload(journey)
    _sync(journey)
    assert _task(journey, f"upload-quality:{journey.journey_id}:{batch}")["task_status"] == "READY"
    assert _gate_tasks(journey) == ["PC_PAGE_QUALITY_REJECTED"]


def test_moving_from_the_old_status_task_keeps_the_refused_page_covered(journey):
    """Review of #479: a status task raised by the old code for a file whose
    only failed page was refused for quality closes, and in the same pass
    the quality task opens -- the gate is never left uncovered."""
    from audit_core.uc03_p2_task_producer import _failed_pages_text, raise_or_refresh

    batch, pages = add_batch_pages(journey, [("booking_form", "READY"), (None, "QUEUED")])
    _fail(journey, pages[1]["queue_id"], "DI_QUALITY_IMAGE_BLUR_SCORE")
    _batch_status(journey, batch, "PARTIAL_FAILURE")
    _unrelated_upload(journey)
    old_title, old_text = _failed_pages_text("packet.pdf", ["2|DI_QUALITY_IMAGE_BLUR_SCORE"], [2], None)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        raise_or_refresh(  # what the code before this change left on file
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            dedupe_key=f"upload-status:{journey.journey_id}:{batch}", task_type="PC_UPLOAD_STATUS",
            source_type="DOCUMENT", source_code="DOCUMENT_PROCESSING_FAILED", title=old_title, description=old_text,
            reference={"batchId": str(batch), "filename": "packet.pdf", "pageNumbers": [2]}, severity="HIGH",
            registry=get_registry(),
        )
    assert _gate_tasks(journey) == ["PC_UPLOAD_STATUS"]

    _sync(journey)
    assert _task(journey, f"upload-status:{journey.journey_id}:{batch}")["task_status"] == "VERIFIED_COMPLETE"
    quality = _task(journey, f"upload-quality:{journey.journey_id}:{batch}")
    assert quality["task_status"] == "READY" and quality["reference"]["pageNumbers"] == [2]
    assert _gate_tasks(journey) == ["PC_PAGE_QUALITY_REJECTED"]


def test_a_file_with_only_refused_pages_has_no_status_task(journey):
    batch, pages = add_batch_pages(journey, [(None, "QUEUED")])
    _fail(journey, pages[0]["queue_id"], "DI_QUALITY_IMAGE_BLUR_SCORE")
    _batch_status(journey, batch, "FAILED")
    _sync(journey)
    assert _task(journey, f"upload-status:{journey.journey_id}:{batch}") is None
    assert _task(journey, f"upload-quality:{journey.journey_id}:{batch}")["task_status"] == "READY"


@pytest.mark.parametrize("how", ["same scan uploaded again", "page removed", "file deleted", "page read"])
def test_it_closes_only_when_the_refused_pages_themselves_go(journey, how):
    batch, pages = add_batch_pages(journey, [(None, "QUEUED")])
    _fail(journey, pages[0]["queue_id"], "DI_QUALITY_IMAGE_CONTRAST")
    _sync(journey)
    assert _task(journey, f"upload-quality:{journey.journey_id}:{batch}")["task_status"] == "READY"
    if how == "same scan uploaded again":
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            connection.execute(text("UPDATE auditcore.p2_document_queue SET queue_status='CANCELLED', "
                                    "last_error='SUPERSEDED_PAGE' WHERE queue_id=:q"), {"q": pages[0]["queue_id"]})
    elif how == "page removed":
        _page_status(journey, pages[0]["queue_id"], "CANCELLED")
    elif how == "file deleted":
        _batch_status(journey, batch, "CANCELLED")
    else:
        _page_status(journey, pages[0]["queue_id"], "READY")
    _sync(journey)
    closed = _task(journey, f"upload-quality:{journey.journey_id}:{batch}")
    assert closed["task_status"] == "VERIFIED_COMPLETE"
    assert _gate_tasks(journey) == []


# ----------------------------------------------------------------- the API


def _p2_task(journey, task_type: str, category: str, source_code: str, reference: dict) -> str:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return str(create_p2_task(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, task_type=task_type,
            category=category, origin_kind="SYSTEM", source_type="DOCUMENT", source_code=source_code,
            dedupe_key=f"t:{uuid4()}", title=f"{task_type} task", description="Something to do. More words.",
            reference=reference, severity="HIGH", priority="HIGH", assigned_role_code="PC", assigned_actor_id=None,
            raised_by_actor_id=None, raised_by_role_code=None, allowed_actions=["ADD_COMMENT"],
            completion_protocol="MACHINE_VERIFIED", due_at_utc=datetime.now(UTC) + timedelta(hours=4),
        ))


def test_the_task_list_carries_categories_and_grouped_counts(journey):
    review = _p2_task(journey, "MANUAL_VERIFICATION_REVIEW", "DOCUMENT_VERIFICATION", "MANUAL_VERIFICATION", {})
    quality = _p2_task(journey, "PC_PAGE_QUALITY_REJECTED", "DOCUMENT_REUPLOAD", "DOCUMENT_QUALITY_REJECTED",
                       {"filename": "scan.pdf", "pageNumbers": [2],
                        "rejections": [{"reason": "blurred", "pages": [2]}]})
    sku = _p2_task(journey, "SKU_CLOSEST_MATCH_CONFIRM", "CORRECTION_APPROVAL", "SKU", {})
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/tasks"
    body = client.get(base, params={"view": "open"}).json()
    rows = {row["task_id"]: row for row in body["items"]}
    assert rows[review]["document_category"] == "MANUAL_VERIFICATION" and rows[review]["queue_tab"] == "DOCUMENTS"
    assert rows[quality]["document"] == {
        "category": "IMAGE_QUALITY", "categoryLabel": "Image quality", "file": "scan.pdf", "pages": [2],
        "reason": "Page 2 is blurred. Re-scan and upload again.", "documentId": None,
    }
    # Not document work: unchanged.
    assert rows[sku]["document_category"] is None and rows[sku]["document"] is None
    assert rows[sku]["queue_tab"] == "MANUAL_VERIFICATION"
    assert body["counts"] == {"ALL": 3, "DOCUMENTS": 2, "MANUAL_VERIFICATION": 1}
    assert body["documentCounts"] == {"TOTAL": 2, "MANUAL_VERIFICATION": 1, "FAILED_TO_READ": 0,
                                      "IMAGE_QUALITY": 1, "MISSING_DOCUMENT": 0}
    assert body["documentCountsByJourney"] == {str(journey.journey_id): body["documentCounts"]}

    only = client.get(base, params={"documentCategory": "image_quality"}).json()
    assert [row["task_id"] for row in only["items"]] == [quality]
    assert only["documentCounts"]["TOTAL"] == 2  # the counts stay whole

    per_journey = client.get(base, params={"journey_id": str(journey.journey_id), "tab": "documents"}).json()
    assert sorted(row["task_id"] for row in per_journey["items"]) == sorted([review, quality])
