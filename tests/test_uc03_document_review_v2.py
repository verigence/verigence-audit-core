from fastapi.routing import APIRoute

# Importing this module is what registers GET /uc03/documents/review -- see
# UnifiedReviewV2Response's own comment in uc03_document_review_v2.py for
# why this route's mere existence is the regression this test guards
# against: a Delivery review read never existed as a route at all before
# (GET /delivery/review 404'd, unconditionally, for every Journey), and
# GET /booking/review was a second, separate call for the other stage on
# every single page load.
from audit_core import uc03_confidence_review_policy  # noqa: F401
from audit_core.uc03_document_review_v2 import (
    UnifiedReviewV2Response,
    _field_review_state,
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
