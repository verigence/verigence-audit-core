from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from audit_core.uc03_pc_verification import _review_readiness


def test_pc_verification_review_readiness_executes_on_fresh_schema() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 PC verification integration test")

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            result = _review_readiness(
                connection,
                tenant_id=f"tenant-pc-verification-{uuid4().hex}",
                journey_id=uuid4(),
            )
        assert result == {
            "linkedDocumentCount": 0,
            "pendingDocumentCount": 0,
            "failedDocumentCount": 0,
            "pendingProposalCount": 0,
            "reviewReady": False,
        }
    finally:
        engine.dispose()
