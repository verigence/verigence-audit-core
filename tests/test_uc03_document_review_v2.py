import os
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import create_engine

# Importing this module is what registers GET /uc03/documents/review -- see
# UnifiedReviewV2Response's own comment in uc03_document_review_v2.py for
# why this route's mere existence is the regression this test guards
# against: a Delivery review read never existed as a route at all before
# (GET /delivery/review 404'd, unconditionally, for every Journey), and
# GET /booking/review was a second, separate call for the other stage on
# every single page load.
from audit_core import uc03_confidence_review_policy  # noqa: F401
from audit_core.db import set_tenant_context
from audit_core.uc03_document_review_v2 import (
    UnifiedReviewV2Response,
    _field_review_state,
    _v2_documents_for_stage,
    router,
)


def test_review_field_at_90_is_ready() -> None:
    assert _field_review_state(value="ABC", confidence_score=90.0) == "READY"


def test_review_field_above_90_is_ready() -> None:
    assert _field_review_state(value="ABC", confidence_score=98.5) == "READY"


def test_review_field_below_90_needs_review() -> None:
    assert _field_review_state(value="ABC", confidence_score=89.99) == "NEEDS_REVIEW"


def test_review_field_without_confidence_needs_review() -> None:
    assert _field_review_state(value="ABC", confidence_score=None) == "NEEDS_REVIEW"


def test_empty_high_confidence_field_is_not_a_review_exception() -> None:
    # UC03 review work is exception-only for populated DI facts below 90%.
    assert _field_review_state(value=None, confidence_score=99.0) == "READY"


def test_unified_review_route_is_registered() -> None:
    routes = [
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path.endswith("/uc03/documents/review")
        and "GET" in route.methods
    ]
    assert len(routes) == 1
    assert routes[0].response_model is UnifiedReviewV2Response


def test_old_separate_review_routes_are_gone() -> None:
    # The two previously-separate stage reads (GET /booking/review existed,
    # GET /delivery/review never did) are both replaced outright by the one
    # unified route above -- guards against either reappearing alongside it.
    paths = {
        route.path
        for route in router.routes
        if isinstance(route, APIRoute) and "GET" in route.methods
    }
    assert not any(path.endswith("/booking/review") for path in paths)
    assert not any(path.endswith("/delivery/review") for path in paths)


class _ExplodingV2Client:
    """Fails the test if list_documents is ever called -- used to prove the
    DI round trip is skipped when there's nothing locally to justify it."""

    def list_documents(self, **kwargs):
        raise AssertionError("list_documents must not be called with no local documents")


def test_v2_documents_for_stage_skips_di_call_with_no_local_documents() -> None:
    """Root-caused live (2026-09-26): a brand-new journey's first Documents
    page load called DI's list_documents for both BOOKING and DELIVERY
    unconditionally, even though document_capture_v2_documents -- the sole
    local record of anything ever uploaded -- had zero rows for either
    stage, guaranteeing DI had nothing classified to return either. This
    call alone carries a ~15s timeout budget; skipping it when local state
    already proves the answer is empty was a direct fix for a confirmed
    12.8s /uc03/documents/review response."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    tenant_id = f"tenant-vdfs-{uuid4().hex[:10]}"
    journey_id = uuid4()

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        documents = _v2_documents_for_stage(
            connection=connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            token="fake-token",
            context_ref="fake-context",
            di_client=None,
            v2_client=_ExplodingV2Client(),
            stage="DELIVERY",
            requirements=None,
        )
    assert documents == []
    engine.dispose()
