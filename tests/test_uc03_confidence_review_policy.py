from __future__ import annotations

import inspect
from uuid import uuid4

import audit_core.uc03_confidence_review_policy as confidence_policy
from audit_core import uc03_document_review_v2 as review_v2
from audit_core.uc03_confidence_review_policy import (
    REVIEW_THRESHOLD_PERCENT,
    _build_raw_review_item,
    _field_review_state,
    acknowledge_booking_document_link_with_auto_sync,
    requires_pc_review,
)


def _raw(*, value: str, confidence: float | None, document_label: str):
    return review_v2.ReviewV2UnmappedField(
        canonicalFieldId=str(uuid4()),
        fieldKey="future_business_field",
        value=value,
        confidenceScore=confidence,
        sourceFactVersion=1,
        documentId=uuid4(),
        documentTypeKey="booking_form",
        documentLabel=document_label,
        originalFilename=f"{document_label}.pdf",
        pageNo=1,
        evidenceRegion=None,
    )


def test_review_threshold_is_exactly_ninety_percent() -> None:
    assert REVIEW_THRESHOLD_PERCENT == 90.0
    assert requires_pc_review(89.99) is True
    assert requires_pc_review(90.0) is False
    assert requires_pc_review(100.0) is False
    assert requires_pc_review(None) is True


def test_field_review_state_depends_on_confidence_not_value_presence() -> None:
    assert _field_review_state(value=None, confidence_score=95.0) == "READY"
    assert _field_review_state(value="value", confidence_score=89.0) == "NEEDS_REVIEW"


def test_conflicting_high_confidence_sources_do_not_create_pc_review_work() -> None:
    item = _build_raw_review_item(
        "raw:future_business_field",
        [
            _raw(value="A", confidence=96.0, document_label="Document A"),
            _raw(value="B", confidence=94.0, document_label="Document B"),
        ],
    )
    assert item is not None
    assert item.decision_required is False


def test_any_low_confidence_source_creates_pc_review_work() -> None:
    item = _build_raw_review_item(
        "raw:future_business_field",
        [
            _raw(value="A", confidence=96.0, document_label="Document A"),
            _raw(value="B", confidence=88.0, document_label="Document B"),
        ],
    )
    assert item is not None
    assert item.decision_required is True


def test_document_link_webhook_defers_sync_to_a_background_task() -> None:
    # DI's own client enforces a hard 5s timeout on this callback -- a
    # consistent timeout retried indefinitely against the same document was
    # observed live. The webhook must acknowledge the link and return
    # immediately; the DI fact fetch / durable copy / SKU resolution /
    # reconciliation / materialization pipeline runs afterward, off the
    # response path, regardless of how slow it gets.
    source = inspect.getsource(acknowledge_booking_document_link_with_auto_sync)
    assert "background_tasks.add_task(" in source
    assert "_run_sync_booking_document_task" in source
    assert "_sync_booking_document(" not in source


def test_sync_booking_document_serializes_per_journey_with_an_advisory_lock() -> None:
    # Multiple documents for the same journey can now confirm close together
    # -- an upload batch, or several background syncs firing in quick
    # succession once the webhook responds immediately (see
    # _run_sync_booking_document_task) -- all writing the same
    # journey_stage_states/evidence rows. Racing them was observed live as a
    # Postgres statement-timeout under row-lock contention; this lock must
    # be the very first thing the pipeline does, before it touches any of
    # those rows, so concurrent callers queue instead of colliding.
    #
    # Read straight from the source file rather than inspect.getsource() on
    # the live name: install_uc03_post_extraction_materialization() (a
    # different, unrelated monkey-patch) reassigns
    # confidence_policy._sync_booking_document at app startup, and whichever
    # test file's TestClient(app) happens to run first during collection
    # decides whether a plain `from ... import _sync_booking_document` here
    # captures the original function or that wrapper -- order-dependent
    # either way, so don't rely on the live object at all.
    source_file = inspect.getsourcefile(confidence_policy)
    assert source_file is not None
    with open(source_file) as f:
        module_source = f.read()
    start = module_source.index("\ndef _sync_booking_document(")
    end = module_source.index("\ndef ", start + 1)
    function_source = module_source[start:end]
    assert "pg_advisory_xact_lock" in function_source
    assert (
        function_source.index("pg_advisory_xact_lock")
        < function_source.index("FROM auditcore.evidence")
    )


def test_background_sync_task_gives_itself_headroom_past_the_pool_default_timeout() -> None:
    # Regression: the per-journey advisory lock above (added specifically to
    # stop QueryCanceled: canceling statement due to statement timeout) can
    # still hit that exact same error once a real upload batch is large
    # enough -- confirmed live at 15 Delivery documents -- because each
    # document's turn under the lock now includes a DI network round trip,
    # and the connection pool's statement_timeout (dependencies.py, 10s) is
    # tuned for interactive HTTP requests, not a serialized background
    # queue. This background task has no HTTP client waiting on it, so it
    # must give its own transaction real headroom instead of inheriting the
    # tight default.
    source_file = inspect.getsourcefile(confidence_policy)
    assert source_file is not None
    with open(source_file) as f:
        module_source = f.read()
    start = module_source.index("\ndef _run_sync_booking_document_task(")
    end = module_source.index("\ndef ", start + 1)
    function_source = module_source[start:end]
    assert "SET LOCAL statement_timeout" in function_source
    assert (
        function_source.index("SET LOCAL statement_timeout")
        < function_source.index("_sync_booking_document(")
    )


def test_confirm_calls_attribute_resolution_directly_not_via_review_v2() -> None:
    # Regression test for a live production AttributeError: confirm_booking_
    # review_v2_confidence_policy used to call review_v2.apply_supported_
    # operational_attribute(...) / review_v2.record_attribute_resolution(...),
    # relying on uc03_document_review_v2.py having imported those two names
    # into its own module namespace. That import was removed on 2026-08-31
    # when confirm_booking_review_v2 (the handler that originally called them
    # directly) moved out of uc03_document_review_v2.py entirely -- leaving
    # this confirm handler's own later call sites pointing at a module
    # attribute that no longer exists. Nothing caught it because no test
    # exercises this handler's actual execute() body (it needs a full
    # attributes/documents/decisions fixture); it surfaced only once a real
    # Booking confirm reached a populated, SUPPORTED-mapping attribute.
    assert hasattr(confidence_policy, "apply_supported_operational_attribute")
    assert hasattr(confidence_policy, "record_attribute_resolution")

    source = inspect.getsource(confidence_policy.confirm_booking_review_v2_confidence_policy)
    assert "review_v2.apply_supported_operational_attribute" not in source
    assert "review_v2.record_attribute_resolution" not in source
    assert "apply_supported_operational_attribute(" in source
    assert "record_attribute_resolution(" in source
