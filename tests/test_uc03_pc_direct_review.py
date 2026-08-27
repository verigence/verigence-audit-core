from __future__ import annotations

import inspect
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from audit_core.uc03_pc_direct_review import (
    DirectDocumentReviewCommand,
    _reviewed_document_ids,
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


def test_reviewed_document_lookup_executes_on_fresh_postgres_schema() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 direct review integration test")

    engine = create_engine(database_url)
    document_id = uuid4()
    try:
        with engine.begin() as connection:
            reviewed = _reviewed_document_ids(
                connection,
                tenant_id=f"tenant-direct-review-{uuid4().hex}",
                journey_id=uuid4(),
                active_document_ids=[document_id],
            )
        assert reviewed == []
    finally:
        engine.dispose()
