"""Pure-function coverage for the Task Queue classification/title fix:
Task rows previously left item.findingClass null (only Findings ever had
one), so they were invisible to every Documents/Data/Violations class
filter while _load_task_queue applied no class filter of its own -- every
open Task showed up under every class tab, and none of them counted toward
any tab's own badge. _tasks_to_items/_task_specific_suffix take plain row
mappings and no DB, so this is tested at the direct-function-call level,
no live connection needed.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import audit_core.uc03_review_queue as rq


def _task_row(*, task_type: str, payload: dict, journey_id=None, **overrides) -> dict:
    base = {
        "workflow_task_id": uuid4(),
        "journey_id": journey_id or uuid4(),
        "journey_reference": "J-1",
        "process_area": "BOOKING",
        "task_type": task_type,
        "task_status": "PENDING",
        "severity": "MEDIUM",
        "assigned_role_code": "PC",
        "assigned_actor_id": None,
        "priority": None,
        "due_at_utc": None,
        "created_at_utc": datetime.now(UTC),
        "version_no": 1,
        "related_finding_id": None,
        "task_payload": payload,
        "customer_name": "Cust",
        "dealer_name": "Dealer",
        "outlet_name": "Outlet",
        "product_label": None,
    }
    base.update(overrides)
    return base


# ── _task_specific_suffix ──────────────────────────────────────────────────────
def test_specific_suffix_prefers_field_over_document_for_data_gap() -> None:
    payload = {"documentLabel": "Booking Docket", "fieldKeys": ["engine_number"]}
    assert rq._task_specific_suffix(payload, derived_class="DATA_GAP") == "Booking Docket: Engine Number"


def test_specific_suffix_caps_multiple_field_names() -> None:
    payload = {"documentLabel": "Booking Docket", "fieldKeys": ["engine_number", "chassis_number", "colour"]}
    assert rq._task_specific_suffix(payload, derived_class="DATA_GAP") == "Booking Docket: Engine Number +2 more"


def test_specific_suffix_uses_document_name_for_document_gap() -> None:
    payload = {"documentTypeKey": "dealer_receipt"}
    assert rq._task_specific_suffix(payload, derived_class="DOCUMENT_GAP") == "Dealer Receipt"


def test_specific_suffix_falls_back_through_payload_shapes() -> None:
    assert rq._task_specific_suffix({"originalFilename": "scan.pdf"}, derived_class="DOCUMENT_GAP") == "Scan.Pdf"
    assert rq._task_specific_suffix({"provider_name": "hdfc_bank"}, derived_class="DATA_GAP") == "Hdfc Bank"


def test_specific_suffix_none_when_payload_has_nothing_specific() -> None:
    assert rq._task_specific_suffix({"diDocumentId": str(uuid4())}, derived_class="DOCUMENT_GAP") is None


# ── _tasks_to_items: classification + filtering ────────────────────────────────
def test_manual_verification_task_gets_data_gap_class_and_specific_title() -> None:
    rows = [
        _task_row(
            task_type="MANUAL_VERIFICATION_REVIEW",
            payload={
                "ruleKey": f"MANUAL_VERIFICATION:BOOKING:{uuid4()}",
                "documentLabel": "Booking Docket",
                "fieldKeys": ["engine_number"],
                "comment": "1 machine-read value on Booking Docket (Engine Number) is below the 90% confidence threshold.",
            },
        )
    ]
    items = rq._tasks_to_items(rows, roles=["PC"], now=datetime.now(UTC), subject_kind="JOURNEY")
    assert len(items) == 1
    item, _ = items[0]
    assert item.findingClass == "DATA_GAP"
    assert item.title == "Verify low-confidence fields — Booking Docket: Engine Number"


def test_duplicate_receipt_task_gets_document_gap_class() -> None:
    rows = [_task_row(task_type="DUPLICATE_RECEIPT_NOTICE", payload={"documentTypeKey": "dealer_receipt"})]
    items = rq._tasks_to_items(rows, roles=["PC"], now=datetime.now(UTC), subject_kind="JOURNEY")
    item, _ = items[0]
    assert item.findingClass == "DOCUMENT_GAP"
    assert item.title == "Duplicate receipt -- won't be counted — Dealer Receipt"


def test_auto_self_serve_is_not_in_the_class_map_and_falls_back_to_plain_title() -> None:
    # AUTO_SELF_SERVE never reaches this function in production (excluded by
    # _TASK_QUEUE_SQL itself), but _tasks_to_items is defensive regardless:
    # an unmapped task_type gets findingClass=None and its plain title, not
    # a crash or a wrong guess.
    rows = [_task_row(task_type="AUTO_SELF_SERVE", payload={"ruleKey": "MODEL_NOT_IDENTIFIED"})]
    items = rq._tasks_to_items(rows, roles=["PC"], now=datetime.now(UTC), subject_kind="JOURNEY")
    item, _ = items[0]
    assert item.findingClass is None
    assert item.title == "Select the vehicle SKU"


def test_finding_class_filter_excludes_tasks_of_a_different_derived_class() -> None:
    rows = [
        _task_row(task_type="MANUAL_VERIFICATION_REVIEW", payload={"documentLabel": "Booking Docket"}),
        _task_row(task_type="DUPLICATE_RECEIPT_NOTICE", payload={"documentTypeKey": "dealer_receipt"}),
    ]
    # This is the exact bug: before the fix, both rows above would show up
    # under every class filter, including one asking only for DOCUMENT_GAP.
    doc_only = rq._tasks_to_items(
        rows, roles=["PC"], now=datetime.now(UTC), subject_kind="JOURNEY", finding_class="DOCUMENT_GAP"
    )
    assert [item.category for item, _ in doc_only] == ["DUPLICATE_RECEIPT_NOTICE"]

    data_only = rq._tasks_to_items(
        rows, roles=["PC"], now=datetime.now(UTC), subject_kind="JOURNEY", finding_class="DATA_GAP"
    )
    assert [item.category for item, _ in data_only] == ["MANUAL_VERIFICATION_REVIEW"]
