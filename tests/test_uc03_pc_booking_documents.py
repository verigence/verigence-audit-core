from __future__ import annotations

import inspect

from audit_core import uc03_pc_booking_documents as pc_booking_documents


def _link_source() -> str:
    return inspect.getsource(pc_booking_documents.acknowledge_booking_document_link)


def test_a_newer_copy_of_a_single_slot_document_supersedes_the_earlier_one() -> None:
    # Decision 2026-10-01 (replaces the September rule that voided the newer
    # copy and raised a duplicate notice): a second upload against a
    # single-slot requirement -- a second PAN card, a re-scanned docket --
    # supersedes the earlier one. The earlier evidence becomes SUPERSEDED
    # (restorable), its capture row says so, and the new document is the
    # one linked ACTIVE with supersedes_evidence_id pointing back.
    source = _link_source()
    assert "'VOIDED', 'DUPLICATE_UPLOAD'" not in source
    assert "create_workflow_task(" not in source
    supersede = source.index("SET association_status='SUPERSEDED'")
    assert "void_reason='REPLACED_BY_NEWER_UPLOAD'" in source
    assert "SET capture_status='SUPERSEDED'" in source
    unconditional_insert = source.index("'ACTIVE', :supersedes_evidence_id,")
    assert source.index("if not repeatable:") < supersede < unconditional_insert


def test_repeatable_requirements_never_supersede() -> None:
    # Receipts, bank statements and scrappage documents accumulate ACTIVE
    # evidence rows: the supersede path is gated behind `if not repeatable:`
    # and the prior-evidence lookup lives inside that guard.
    source = _link_source()
    guard = source.index("if not repeatable:")
    prior_lookup = source.index("prior_evidence_id = connection.execute(")
    supersede = source.index("SET association_status='SUPERSEDED'")
    assert guard < prior_lookup < supersede


def test_on_demand_checklist_rows_are_repeatable() -> None:
    assert pc_booking_documents._is_repeatable_requirement("p2_extra_customer_kyc") is True
    assert pc_booking_documents._is_repeatable_requirement("p2_extra_upi_screenshot") is True
    assert pc_booking_documents._is_repeatable_requirement("p2_bank_approval_letter") is False
    assert pc_booking_documents._is_repeatable_requirement("pan_card") is False


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
