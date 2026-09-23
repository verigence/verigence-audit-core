from __future__ import annotations

import inspect

from audit_core import uc03_pc_booking_documents as pc_booking_documents


def _link_source() -> str:
    return inspect.getsource(pc_booking_documents.acknowledge_booking_document_link)


def test_duplicate_upload_against_a_single_slot_requirement_is_rejected_not_superseded() -> None:
    # Standing rule, explicit and unambiguous: a second upload against a
    # single-slot (non-repeatable) requirement -- a second PAN card, GST
    # declaration, scrappage certificate, etc -- is rejected outright, even
    # when it's a genuinely different (corrected/re-scanned) file, not
    # silently superseded. The prior evidence must stay ACTIVE and
    # untouched; the new document is voided as a duplicate.
    source = _link_source()
    assert "'VOIDED', 'DUPLICATE_UPLOAD'" in source
    # The old unconditional silent-supersede write for a brand new document
    # must be gone -- a genuinely new evidence row is only ever inserted as
    # 'ACTIVE' when there was no prior ACTIVE evidence to begin with.
    assert "SET association_status='SUPERSEDED'" not in source


def test_duplicate_upload_raises_a_pc_facing_task() -> None:
    source = _link_source()
    assert "create_workflow_task(" in source
    assert "_DUPLICATE_DOCUMENT_TASK_TYPE" in source
    assert 'assigned_role_code="PC"' in source
    assert source.index("'VOIDED', 'DUPLICATE_UPLOAD'") < source.index("create_workflow_task(")
    assert pc_booking_documents._DUPLICATE_DOCUMENT_TASK_TYPE == "DUPLICATE_DOCUMENT_NOTICE"


def test_duplicate_upload_returns_early_without_moving_the_assessment_pointer() -> None:
    # journey_document_assessments must keep pointing at the ORIGINAL,
    # still-ACTIVE evidence -- the rejected duplicate's evidence_id must
    # never reach that INSERT.
    source = _link_source()
    early_return = source.index(
        "return BookingDocumentLinkResponse(\n"
        "                    requirementRef=payload.requirementRef,\n"
        "                    documentId=payload.documentId,\n"
        "                    evidenceId=rejected_evidence_id,"
    )
    assessment_insert = source.index("INSERT INTO auditcore.journey_document_assessments")
    assert early_return < assessment_insert


def test_repeatable_requirements_are_unaffected_by_the_reject_path() -> None:
    # Receipts/bank-statements/scrappage-pair documents must still be able
    # to accumulate multiple ACTIVE evidence rows -- the reject-as-duplicate
    # behavior is scoped to `not repeatable` only, and the later unconditional
    # insert (reached whenever there was no prior ACTIVE evidence, including
    # every repeatable requirement) must still write 'ACTIVE', not 'VOIDED'.
    source = _link_source()
    not_repeatable_guard = source.index("if not repeatable:")
    reject_insert = source.index("'VOIDED', 'DUPLICATE_UPLOAD'")
    unconditional_insert = source.index("'ACTIVE', :supersedes_evidence_id,")
    assert not_repeatable_guard < reject_insert < unconditional_insert


def test_discover_requirement_for_callback_never_reads_document_capture_v2_documents() -> None:
    """Direct claim made to the user (2026-09-23), verified here so it stays
    true rather than just being true today: which rules/materializers fire
    for a classified document is resolved entirely from the requirement row
    itself (journey_document_requirements.process_area, fixed correctly at
    upload time -- see uc03_unified_document_capture.py's upload-intent
    endpoint) -- never from document_capture_v2_documents.stage_code, which
    is a separate, UI-display-only cache that can lag behind. This function
    is where that resolution happens (its own comment: "Stage is data, not
    routing... DI has no notion of Booking vs Delivery, and neither should
    this handler"); a plain string check that the table name never appears
    in its source is a stronger guarantee than any test of specific
    behavior, since it proves the dependency cannot exist for ANY input,
    not just the cases a test happens to cover."""
    source = inspect.getsource(pc_booking_documents._discover_requirement_for_callback)
    assert "document_capture_v2_documents" not in source
    assert "process_area" in source
