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
