from __future__ import annotations

import inspect
from uuid import uuid4

from audit_core.uc03_pc_direct_review import (
    DirectDocumentReviewCommand,
    submit_direct_document_review,
    verify_pc_booking_direct,
)


def test_direct_document_review_allows_zero_mapped_fields() -> None:
    command = DirectDocumentReviewCommand(
        requirementRef=uuid4(),
        documentId=uuid4(),
        fields=[],
    )
    assert command.fields == []


def test_direct_document_review_records_document_completion() -> None:
    source = inspect.getsource(submit_direct_document_review)
    assert 'event_type="BOOKING_DOCUMENT_REVIEWED"' in source
    assert '"reviewedFieldCount": len(payload.fields)' in source


def test_direct_document_review_does_not_mutate_booking_or_audit_status() -> None:
    source = inspect.getsource(submit_direct_document_review)
    assert "SET business_status=" not in source
    assert "audit_state=CASE" not in source
    assert "SET latest_activity_at_utc=now()" in source


def test_direct_pc_verification_only_changes_pc_verification_state() -> None:
    source = inspect.getsource(verify_pc_booking_direct)
    assert "SET pc_verification_status='VERIFIED'" in source
    assert "business_status" not in source
    assert "audit_state" not in source
    assert "reviewComplete" in source
